"""Сквозная проверка контура фазы 1.

Обновление проходит весь путь — HTTP-запрос вебхука, фоновая задача,
диспетчер aiogram — и превращается в вызов sendMessage с ответом «понг».
Сеть при этом не используется: сессия Telegram подменена.

Это доказательство того, что критерий «бот отвечает понг» выполнен не на
уровне «вроде бы собралось», а на уровне реально проходящего обновления.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.core import texts
from app.infra.server import TELEGRAM_SECRET_HEADER, create_app
from app.infra.tasks import BackgroundTasks

SECRET = "integration-webhook-secret"
PATH = "/webhook/telegram"
CHAT_ID = 555
WebClient = TestClient[web.Request, web.Application]


class RecordingSession(BaseSession):
    """Сессия Telegram, которая ничего не отправляет, но всё запоминает."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура из aiogram
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                text=method.text,
            ).as_(bot)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура из aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None


def _text_update(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Тест"},
            "text": text,
        },
    }


@dataclass
class Harness:
    """Поднятое приложение вместе с подменённой сессией Telegram."""

    client: WebClient
    session: RecordingSession
    tasks: BackgroundTasks

    async def post(self, update: dict[str, Any]) -> int:
        """Шлёт обновление с правильным секретом и ждёт обработки."""
        response = await self.client.post(
            PATH, json=update, headers={TELEGRAM_SECRET_HEADER: SECRET}
        )
        await self.tasks.drain(timeout=2.0)
        return response.status

    def sent_messages(self) -> list[SendMessage]:
        return [call for call in self.session.calls if isinstance(call, SendMessage)]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    # Импорт внутри фикстуры: build_dispatcher относится к сборке приложения,
    # и держать его импорт рядом с остальными незачем.
    from app.main import build_dispatcher

    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = build_dispatcher()
    tasks = BackgroundTasks()

    async def handle_update(raw: dict[str, Any]) -> None:
        await dispatcher.feed_raw_update(bot, raw)

    app = create_app(
        telegram_secret=SECRET,
        telegram_webhook_path=PATH,
        handle_update=handle_update,
        tasks=tasks,
        health=lambda: {"status": "ok", "in_flight": tasks.running},
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(client=client, session=session, tasks=tasks)

    await tasks.drain(timeout=2.0)
    await client.close()


async def test_any_message_gets_pong(harness: Harness) -> None:
    assert await harness.post(_text_update(1, "привет")) == 200

    sent = harness.sent_messages()
    assert len(sent) == 1
    assert sent[0].text == texts.PONG
    assert sent[0].chat_id == CHAT_ID


async def test_unauthorized_update_never_reaches_the_bot(harness: Harness) -> None:
    """Чужой запрос не должен доходить до бота вообще."""
    response = await harness.client.post(PATH, json=_text_update(2, "привет"))
    await harness.tasks.drain(timeout=2.0)

    assert response.status == 403
    assert harness.session.calls == []


async def test_every_message_gets_its_own_answer(harness: Harness) -> None:
    """Два обновления — два ответа, ничего не потерялось и не задвоилось."""
    await harness.post(_text_update(3, "первое"))
    await harness.post(_text_update(4, "второе"))

    assert [message.text for message in harness.sent_messages()] == [
        texts.PONG,
        texts.PONG,
    ]
