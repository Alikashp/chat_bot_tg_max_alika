"""Точка входа: сборка зависимостей и запуск сервиса.

Здесь и только здесь конкретные реализации соединяются с портами. Всё, что
выше по стеку, знает лишь протоколы: сценарии не подозревают ни про aiogram,
ни про httpx, ни про PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.ai.http import create_client
from app.adapters.ai.images import OpenAIImages
from app.adapters.ai.resilience import ProviderPolicy, ResilientCaller
from app.adapters.ai.text import OpenAICompatibleLLM
from app.adapters.storage.migrations import upgrade_to_head_async
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.telegram import router as telegram_router
from app.adapters.telegram.intake import dedup_key
from app.adapters.telegram.messenger import TelegramMessenger
from app.config import Settings, get_settings
from app.core.scenarios.deps import Deps
from app.core.settings import CoreSettings
from app.infra.antiflood import FloodGuard
from app.infra.dedup import Deduplicator
from app.infra.logging import configure_logging, get_logger
from app.infra.queue import JobQueue
from app.infra.retry import RetryPolicy
from app.infra.server import Outcome, create_app

logger = get_logger(__name__)

#: Таймаут вызовов к Telegram Bot API. Без явного значения aiogram ждёт 60 с
#: (§3.4.6 требует явный таймаут на каждый HTTP-вызов).
TELEGRAM_API_TIMEOUT = 15.0

#: Что бот получает на вебхук. Чем уже список, тем меньше мусорного трафика.
#: callback_query здесь обязателен: без него нажатия inline-кнопок просто не
#: доходят, и это выглядит как «кнопки не работают».
ALLOWED_UPDATES = ["message", "callback_query"]


@dataclass(slots=True)
class Wiring:
    """Собранное приложение и всё, что при остановке нужно закрыть."""

    bot: Bot
    dispatcher: Dispatcher
    engine: AsyncEngine
    http_clients: tuple[httpx.AsyncClient, ...]


def _utc_now() -> datetime:
    """Часы приложения. Всегда с зоной: наивное время ломает счёт суток."""
    return datetime.now(UTC)


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


def build_providers(
    settings: Settings,
) -> tuple[OpenAICompatibleLLM, OpenAIImages, tuple[httpx.AsyncClient, ...]]:
    """Создаёт провайдеров ИИ вместе с их обвязкой.

    Клиентов два, а не один: у текста и картинок разные таймауты, и общий
    клиент означал бы либо минуту ожидания текста, либо обрыв картинки на
    середине.
    """
    llm_client = create_client(settings.llm_timeout_seconds)
    image_client = create_client(settings.image_timeout_seconds)

    llm = OpenAICompatibleLLM(
        llm_client,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        caller=ResilientCaller(
            "llm",
            ProviderPolicy(
                rate=settings.llm_rate_per_second,
                burst=settings.llm_burst,
                concurrency=settings.llm_concurrency,
            ),
        ),
        system_prompt=settings.llm_system_prompt,
        max_tokens=settings.llm_max_tokens,
    )

    images = OpenAIImages(
        image_client,
        base_url=settings.image_base_url,
        api_key=settings.images_api_key,
        caller=ResilientCaller(
            "images",
            ProviderPolicy(
                rate=settings.image_rate_per_second,
                burst=settings.image_burst,
                concurrency=settings.image_concurrency,
                retry=RetryPolicy(attempts=settings.image_retry_attempts),
                # Таймаут не значит, что на той стороне ничего не нарисовали.
                # Повтор рискует оплатить одну картинку дважды, а пользователь
                # от отказа не теряет ничего: лимит списывается только за
                # доставленный результат.
                retry_on_timeout=False,
            ),
        ),
        model=settings.image_model,
        size=settings.image_size,
    )
    return llm, images, (llm_client, image_client)


def build_core_settings(settings: Settings, bot_username: str) -> CoreSettings:
    """Продуктовые настройки ядра из настроек приложения."""
    return CoreSettings(
        bot_username=bot_username,
        model_economy=settings.model_economy,
        model_standard=settings.model_standard,
        dialog_max_turns=settings.dialog_max_turns,
        max_photo_bytes=settings.max_photo_bytes,
    )


async def build_wiring(settings: Settings) -> Wiring:
    """Соединяет реализации с портами и собирает диспетчер бота."""
    engine = await _prepare_database(settings)

    bot = Bot(
        token=settings.telegram_bot_token,
        session=AiohttpSession(timeout=TELEGRAM_API_TIMEOUT),
    )
    # Имя бота спрашиваем у самого Telegram, а не заводим переменную
    # окружения: лишняя переменная — это лишний способ разойтись с
    # действительностью, а в ней собираются реферальные ссылки.
    me = await bot.get_me()
    if me.username is None:
        raise RuntimeError("у бота нет username — реферальные ссылки не собрать")
    logger.info("bot_identified", bot_id=me.id)

    llm, images, http_clients = build_providers(settings)

    deps = Deps(
        storage=PostgresStorage(engine),
        messenger=TelegramMessenger(bot),
        llm=llm,
        images=images,
        settings=build_core_settings(settings, me.username),
        logger=get_logger("scenarios"),
        guard=FloodGuard(limit=settings.flood_limit_per_user),
        now=_utc_now,
    )

    return Wiring(
        bot=bot,
        dispatcher=telegram_router.build_dispatcher(deps),
        engine=engine,
        http_clients=http_clients,
    )


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


async def _prepare_database(settings: Settings) -> AsyncEngine:
    """Накатывает миграции и проверяет связь.

    Проверка сразу, а не у первого пользователя: неверный адрес базы должен
    валить старт, а не превращаться в поток ошибок в чате.
    """
    if settings.run_migrations_on_start:
        await upgrade_to_head_async(settings.database_url)
        logger.info("migrations_applied")

    engine = create_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    logger.info("database_ready")
    return engine


async def run() -> None:
    """Поднимает сервис и работает до SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env == "production")

    wiring = await build_wiring(settings)

    async def handle_update(raw_update: dict[str, Any]) -> None:
        """Обрабатывает одно обновление вне HTTP-запроса."""
        await wiring.dispatcher.feed_raw_update(wiring.bot, raw_update)

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
            "database": "ready",
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
        await _register_webhook(wiring.bot, settings)

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
        await wiring.bot.session.close()
        for client in wiring.http_clients:
            await client.aclose()
        await wiring.engine.dispose()

        logger.info("shutdown_complete", abandoned=abandoned)


def main() -> None:
    """Запускает приложение."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
