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

from app.config import Settings, get_settings
from app.core import texts
from app.infra.logging import configure_logging, get_logger
from app.infra.server import create_app
from app.infra.tasks import BackgroundTasks

logger = get_logger(__name__)

#: Таймаут вызовов к Telegram Bot API. Без явного значения aiogram ждёт 60 с
#: (§3.4.6 требует явный таймаут на каждый HTTP-вызов).
TELEGRAM_API_TIMEOUT = 15.0

#: На фазе 1 боту нужны только сообщения. Список расширяется вместе со
#: сценариями: чем он уже, тем меньше мусорного трафика на вебхук.
ALLOWED_UPDATES = ["message"]


def build_dispatcher() -> Dispatcher:
    """Собирает диспетчер aiogram.

    Фаза 1: сквозная проверка контура. Бот отвечает «понг» на любое
    сообщение — этого достаточно, чтобы убедиться, что путь
    «Railway → вебхук → фоновая задача → ответ» работает целиком.
    Реальные сценарии приходят на фазе 4.
    """
    dispatcher = Dispatcher()

    @dispatcher.message()
    async def reply_pong(message: Message) -> None:
        await message.answer(texts.PONG)

    return dispatcher


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


async def run() -> None:
    """Поднимает сервис и работает до SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env == "production")

    bot = Bot(
        token=settings.telegram_bot_token,
        session=AiohttpSession(timeout=TELEGRAM_API_TIMEOUT),
    )
    dispatcher = build_dispatcher()
    tasks = BackgroundTasks()

    async def handle_update(raw_update: dict[str, Any]) -> None:
        """Обрабатывает одно обновление вне HTTP-запроса."""
        await dispatcher.feed_raw_update(bot, raw_update)

    def health() -> dict[str, Any]:
        return {
            "status": "ok" if tasks.accepting else "shutting_down",
            "in_flight": tasks.running,
        }

    app = create_app(
        telegram_secret=settings.telegram_webhook_secret,
        telegram_webhook_path=settings.telegram_webhook_path,
        handle_update=handle_update,
        tasks=tasks,
        health=health,
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)  # noqa: S104

    try:
        await site.start()
        logger.info("server_started", port=settings.port)

        # Порядок именно такой: сокет уже слушает, когда мы говорим Telegram
        # присылать сюда обновления. Наоборот — значит некоторое время
        # получать обновления на адрес, который ещё никто не слушает.
        #
        # Если регистрация не удалась, поднимать сервис бессмысленно: бот не
        # получит ни одного сообщения. Падаем, и Railway перезапускает нас по
        # restartPolicy из railway.json. Осмысленные повторы появятся на
        # фазе 2 вместе с остальной инфраструктурой надёжности.
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
        tasks.stop_accepting()
        await site.stop()
        abandoned = await tasks.drain(settings.shutdown_timeout_seconds)
        await runner.cleanup()
        await bot.session.close()

        logger.info("shutdown_complete", abandoned=abandoned)


def main() -> None:
    """Запускает приложение."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
