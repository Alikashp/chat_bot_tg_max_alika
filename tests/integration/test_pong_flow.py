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
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import TELEGRAM_SECRET_HEADER, create_app

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
    queue: JobQueue[dict[str, Any]]

    async def post(self, update: dict[str, Any]) -> int:
        """Шлёт обновление с правильным секретом и ждёт его обработки."""
        response = await self.client.post(
            PATH, json=update, headers={TELEGRAM_SECRET_HEADER: SECRET}
        )
        await self.settle()
        return response.status

    async def settle(self) -> None:
        """Ждёт, пока очередь опустеет, не останавливая воркеров."""
        assert await self.queue.join(timeout=2.0)

    def sent_messages(self) -> list[SendMessage]:
        return [call for call in self.session.calls if isinstance(call, SendMessage)]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    # Импорт внутри фикстуры: сборка приложения относится к main, и держать
    # эти импорты рядом с остальными незачем.
    from app.main import build_dispatcher, build_intake

    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = build_dispatcher()

    async def handle_update(raw: dict[str, Any]) -> None:
        await dispatcher.feed_raw_update(bot, raw)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "test-updates", handle_update, capacity=50, workers=2
    )
    queue.start()
    dedup = Deduplicator(ttl_seconds=600, max_keys=1000)

    app = create_app(
        telegram_secret=SECRET,
        telegram_webhook_path=PATH,
        submit=build_intake(queue, dedup),
        health=lambda: {"status": "ok"},
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(client=client, session=session, queue=queue)

    await queue.drain(timeout=2.0)
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
    await harness.settle()

    assert response.status == 403
    assert harness.session.calls == []


async def test_redelivered_update_is_answered_once(harness: Harness) -> None:
    """Telegram повторяет доставку — второй ответ пользователю не нужен.

    На «понге» это безобидно, но тот же путь на фазе 5 означал бы вторую
    отрисовку картинки: лишние деньги провайдеру и второй списанный лимит.
    """
    update = _text_update(5, "привет")

    assert await harness.post(update) == 200
    assert await harness.post(update) == 200

    assert len(harness.sent_messages()) == 1


async def test_every_message_gets_its_own_answer(harness: Harness) -> None:
    """Два обновления — два ответа, ничего не потерялось и не задвоилось."""
    await harness.post(_text_update(3, "первое"))
    await harness.post(_text_update(4, "второе"))

    assert [message.text for message in harness.sent_messages()] == [
        texts.PONG,
        texts.PONG,
    ]
