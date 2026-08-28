"""Точка входа: сборка зависимостей и запуск сервиса.

Здесь и только здесь конкретные реализации соединяются с портами. Всё, что
выше по стеку, знает лишь протоколы.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.storage.migrations import upgrade_to_head_async
from app.adapters.storage.postgres import create_engine
from app.adapters.telegram.intake import dedup_key
from app.config import Settings, get_settings
from app.core import texts
from app.infra.dedup import Deduplicator
from app.infra.logging import configure_logging, get_logger
from app.infra.queue import JobQueue
from app.infra.server import Outcome, create_app

logger = get_logger(__name__)

#: Таймаут вызовов к Telegram Bot API. Без явного значения aiogram ждёт 60 с
#: (§3.4.6 требует явный таймаут на каждый HTTP-вызов).
TELEGRAM_API_TIMEOUT = 15.0

#: На фазе 1 боту нужны только сообщения. Список расширяется вместе со
#: сценариями: чем он уже, тем меньше мусорного трафика на вебхук.
#: На фазе 4 сюда добавляется callback_query, иначе нажатия кнопок просто
#: не будут доходить.
ALLOWED_UPDATES = ["message"]


def build_dispatcher() -> Dispatcher:
    """Собирает диспетчер aiogram.

    Фаза 1: сквозная проверка контура. Бот отвечает «понг» на любое
    сообщение — этого достаточно, чтобы убедиться, что путь
    «Railway → вебхук → очередь → воркер → ответ» работает целиком.
    Реальные сценарии приходят на фазе 4.
    """
    dispatcher = Dispatcher()

    @dispatcher.message()
    async def reply_pong(message: Message) -> None:
        await message.answer(texts.PONG)

    return dispatcher


def build_intake(queue: JobQueue[dict[str, Any]], dedup: Deduplicator) -> Any:
    """Собирает функцию приёма обновления для HTTP-обработчика.

    Порядок проверок важен. Сначала дедупликация, потом очередь: повтор не
    должен занимать место в очереди, иначе при ретраях мессенджера ёмкость
    выедается копиями одного и того же обновления.
    """

    def submit(raw_update: dict[str, Any]) -> Outcome:
        key = dedup_key(raw_update)
        if key is None:
            logger.warning("update_without_id", messenger="telegram")
            return Outcome.MALFORMED

        if not dedup.is_new(key):
            logger.info("update_duplicate", messenger="telegram")
            return Outcome.DUPLICATE

        if not queue.accepting:
            return Outcome.STOPPING

        if not queue.submit(raw_update):
            return Outcome.OVERLOADED

        return Outcome.ACCEPTED

    return submit


async def _register_webhook(bot: Bot, settings: Settings) -> None:
    """Регистрирует вебхук при старте.

    Вызывается на каждом запуске: адрес на Railway может смениться, а
    setWebhook идемпотентен. Ожидающие обновления не сбрасываем — их
    доставят после перезапуска.
    """
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
    logger.info("webhook_registered", messenger="telegram")


async def _prepare_database(settings: Settings) -> AsyncEngine | None:
    """Готовит базу, если она настроена.

    Возвращает None, пока DATABASE_URL не задан: до фазы 4 продуктовые
    сценарии к боту не подключены, и хранилище ему не нужно. С фазы 4
    переменная станет обязательной — подписки и лимиты обязаны переживать
    выкатку, а без базы они не переживают даже перезапуск.
    """
    if settings.database_url is None:
        logger.warning("database_not_configured")
        return None

    if settings.run_migrations_on_start:
        await upgrade_to_head_async(settings.database_url)
        logger.info("migrations_applied")

    engine = create_engine(settings.database_url)
    # Проверяем связь сразу: узнать о неверном адресе на старте лучше, чем
    # у первого же пользователя.
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    logger.info("database_ready")
    return engine


async def run() -> None:
    """Поднимает сервис и работает до SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env == "production")

    engine = await _prepare_database(settings)

    bot = Bot(
        token=settings.telegram_bot_token,
        session=AiohttpSession(timeout=TELEGRAM_API_TIMEOUT),
    )
    dispatcher = build_dispatcher()

    async def handle_update(raw_update: dict[str, Any]) -> None:
        """Обрабатывает одно обновление вне HTTP-запроса."""
        await dispatcher.feed_raw_update(bot, raw_update)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "telegram-updates",
        handle_update,
        capacity=settings.queue_capacity,
        workers=settings.queue_workers,
    )
    dedup = Deduplicator(
        ttl_seconds=settings.dedup_ttl_seconds,
        max_keys=settings.dedup_max_keys,
    )

    def health() -> dict[str, Any]:
        stats = queue.stats()
        return {
            "status": "ok" if queue.accepting else "shutting_down",
            "queues": [
                {
                    "name": stats.name,
                    "capacity": stats.capacity,
                    "workers": stats.workers,
                    "pending": stats.pending,
                    "in_flight": stats.in_flight,
                    "accepted": stats.accepted,
                    "rejected": stats.rejected,
                    "failed": stats.failed,
                }
            ],
            "dedup_keys": len(dedup),
            "database": "ready" if engine is not None else "not_configured",
        }

    app = create_app(
        telegram_secret=settings.telegram_webhook_secret,
        telegram_webhook_path=settings.telegram_webhook_path,
        submit=build_intake(queue, dedup),
        health=health,
    )

    queue.start()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)  # noqa: S104

    try:
        await site.start()
        logger.info(
            "server_started",
            port=settings.port,
            capacity=settings.queue_capacity,
            workers=settings.queue_workers,
        )

        # Порядок именно такой: сокет уже слушает, когда мы говорим Telegram
        # присылать сюда обновления. Наоборот — значит некоторое время
        # получать обновления на адрес, который ещё никто не слушает.
        #
        # Если регистрация не удалась, поднимать сервис бессмысленно: бот не
        # получит ни одного сообщения. Падаем, и Railway перезапускает нас по
        # restartPolicy из railway.json.
        await _register_webhook(bot, settings)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("shutdown_started")
    finally:
        # Порядок важен: сначала перестаём брать новую работу, потом закрываем
        # приём соединений, и только потом ждём текущие задачи. Если сделать
        # наоборот, между остановкой сокета и запретом на задачи успеет
        # проскочить новое обновление.
        queue.stop_accepting()
        await site.stop()
        abandoned = await queue.drain(settings.shutdown_timeout_seconds)
        await runner.cleanup()
        await bot.session.close()
        if engine is not None:
            await engine.dispose()

        logger.info("shutdown_complete", abandoned=abandoned)


def main() -> None:
    """Запускает приложение."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
