"""Сквозная проверка MAX-адаптера — и заодно проверка архитектуры.

Те же сценарии, что в Telegram, прогоняются через MAX: HTTP-запрос вебхука →
очередь → адаптер → общий маршрутизатор → сценарий → вызов MAX Bot API.
Настоящее здесь всё, кроме сети MAX.

Смысл файла не в том, что «MAX тоже работает». Смысл в том, что для этого не
понадобилось ни одного продуктового решения на стороне MAX: онбординг, лимиты,
пейволл, рефералка и списание — тот же код, что уже покрыт тестами Telegram.
Это критерий приёмки №10 и A4, проверенные исполнением, а не обещанием.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from maxapi.enums.chat_type import ChatType
from maxapi.enums.upload_type import UploadType
from maxapi.methods.types.sended_message import SendedMessage
from maxapi.types import Message, MessageBody, Recipient
from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload

from app.adapters.max import router as max_router
from app.adapters.max.intake import dedup_key
from app.adapters.max.messenger import MaxMessenger
from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.actions import Action, preset_action
from app.core.limits import current_day
from app.core.models import MessengerKind
from app.core.referral import MAX_HOST
from app.core.scenarios.deps import Deps
from app.core.settings import CoreSettings
from app.infra.antiflood import FloodGuard
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import MAX_SECRET_HEADER, Webhook, create_app
from tests.fakes import (
    PNG_BYTES,
    FakeCards,
    FakeImages,
    FakeLLM,
    FakeLogger,
    FrozenClock,
)


def _today() -> Any:
    """Те же сутки, по которым живут сценарии в этом тесте."""
    return current_day(FrozenClock().now, "Europe/Moscow")


SECRET = "max-integration-secret"
PATH = "/webhook/max"
CHAT_ID = 555
USER_ID = 555
WebClient = TestClient[web.Request, web.Application]


@dataclass
class SentMessage:
    """Что бот отправил в MAX."""

    chat_id: int | None
    text: str | None
    attachments: list[Any]

    @property
    def buttons(self) -> list[list[Any]]:
        for attachment in self.attachments:
            payload = getattr(attachment, "payload", None)
            if payload is not None and hasattr(payload, "buttons"):
                return list(payload.buttons)
        return []

    @property
    def images(self) -> list[Any]:
        return [
            attachment
            for attachment in self.attachments
            if isinstance(attachment, AttachmentUpload)
        ]


class StubMaxBot:
    """Транспорт MAX, который ничего не отправляет, но всё запоминает."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.edits: list[SentMessage] = []
        self.actions: list[str] = []
        self.callbacks: list[str] = []
        self.uploads = 0
        self._next_id = 0

    async def send_message(
        self,
        chat_id: int | None = None,
        text: str | None = None,
        attachments: list[Any] | None = None,
    ) -> SendedMessage:
        self.sent.append(SentMessage(chat_id, text, list(attachments or [])))
        self._next_id += 1
        return SendedMessage(
            message=Message(
                recipient=Recipient(chat_id=chat_id, chat_type=ChatType.DIALOG),
                timestamp=1,
                body=MessageBody(mid=f"mid-{self._next_id}", seq=self._next_id),
                bot=None,
            )
        )

    async def edit_message(
        self,
        message_id: str,
        text: str | None = None,
        attachments: list[Any] | None = None,
    ) -> None:
        self.edits.append(SentMessage(None, text, list(attachments or [])))

    async def send_action(self, chat_id: int | None = None, action: Any = None) -> None:
        self.actions.append(str(action))

    async def send_callback(
        self, callback_id: str, notification: str | None = None
    ) -> None:
        self.callbacks.append(callback_id)

    async def upload_media(self, media: Any) -> AttachmentUpload:
        self.uploads += 1
        return AttachmentUpload(
            type=UploadType.IMAGE,
            payload=AttachmentPayload(token=f"token-{self.uploads}"),
        )


# --- Сборка обновлений ---------------------------------------------------


def start_update(payload: str = "") -> dict[str, Any]:
    return {
        "update_type": "bot_started",
        "timestamp": 1,
        "chat_id": CHAT_ID,
        "user": {"user_id": USER_ID, "first_name": "Тест", "is_bot": False},
        "payload": payload,
    }


def text_update(mid: str, text: str) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": 1,
        "message": {
            "sender": {"user_id": USER_ID, "first_name": "Тест", "is_bot": False},
            "recipient": {"chat_id": CHAT_ID, "chat_type": "dialog"},
            "timestamp": 1,
            "body": {"mid": mid, "seq": 1, "text": text},
        },
    }


def photo_update(mid: str, url: str) -> dict[str, Any]:
    update = text_update(mid, "")
    update["message"]["body"]["text"] = None
    update["message"]["body"]["attachments"] = [
        {"type": "image", "payload": {"url": url, "token": "src", "photo_id": 1}}
    ]
    return update


def press_update(callback_id: str, action: str) -> dict[str, Any]:
    return {
        "update_type": "message_callback",
        "timestamp": 1,
        "message": {
            "recipient": {"chat_id": CHAT_ID, "chat_type": "dialog"},
            "timestamp": 1,
            "body": {"mid": "prev", "seq": 1, "text": "предыдущее"},
        },
        "callback": {
            "timestamp": 1,
            "callback_id": callback_id,
            "payload": action,
            "user": {"user_id": USER_ID, "first_name": "Тест", "is_bot": False},
        },
    }


@dataclass
class Harness:
    client: WebClient
    bot: StubMaxBot
    queue: JobQueue[dict[str, Any]]
    storage: InMemoryStorage
    llm: FakeLLM
    images: FakeImages
    _step: list[int] = field(default_factory=lambda: [0])

    def _next(self) -> str:
        self._step[0] += 1
        return f"mid-in-{self._step[0]}"

    async def post(self, update: dict[str, Any]) -> int:
        response = await self.client.post(
            PATH, json=update, headers={MAX_SECRET_HEADER: SECRET}
        )
        assert await self.queue.join(timeout=5.0)
        return response.status

    async def send_text(self, text: str) -> None:
        assert await self.post(text_update(self._next(), text)) == 200

    async def send_photo(self, url: str = "https://cdn/photo.jpg") -> None:
        assert await self.post(photo_update(self._next(), url)) == 200

    async def press(self, action: str) -> None:
        assert await self.post(press_update(self._next(), action)) == 200

    def texts_said(self) -> list[str]:
        return [message.text for message in self.bot.sent if message.text is not None]

    def forget(self) -> None:
        self.bot.sent.clear()
        self.bot.edits.clear()

    async def user(self) -> Any:
        found = await self.storage.get_user(MessengerKind.MAX, str(USER_ID))
        assert found is not None
        return found


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    from app.main import build_intake

    bot = StubMaxBot()
    clock = FrozenClock()
    storage = InMemoryStorage(clock=clock)
    llm = FakeLLM()
    images = FakeImages()

    def photo_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=PNG_BYTES, headers={"content-type": "image/png"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(photo_response))
    deps = Deps(
        storage=storage,
        messenger=MaxMessenger(bot, http),  # type: ignore[arg-type]
        llm=llm,
        images=images,
        settings=CoreSettings(
            bot_username="testbot",
            referral_link_host=MAX_HOST,
            # Так же собирает настройки MAX и app/main.py.
            show_user_number=True,
            offer_url="https://telegra.ph/offer",
            privacy_url="https://telegra.ph/privacy",
            docs_version="2026-08-31",
        ),
        logger=FakeLogger(),
        guard=FloodGuard(limit=1),
        cards=FakeCards(),
        # Звёзд в MAX нет — и это не пропуск в тесте, а свойство мессенджера.
        stars=None,
        now=clock,
    )

    async def handle(raw: dict[str, Any]) -> None:
        await max_router.handle_update(deps, raw)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "max-test", handle, capacity=50, workers=2
    )
    queue.start()

    app = create_app(
        webhooks=[
            Webhook(
                messenger="max",
                path=PATH,
                secret_header=MAX_SECRET_HEADER,
                secret=SECRET,
                submit=build_intake(
                    queue,
                    Deduplicator(ttl_seconds=600, max_keys=1000),
                    messenger="max",
                    key_of=dedup_key,
                ),
            )
        ],
        health=lambda: {"status": "ok"},
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(
        client=client, bot=bot, queue=queue, storage=storage, llm=llm, images=images
    )

    await queue.drain(timeout=2.0)
    await client.close()
    await http.aclose()


@pytest.fixture
async def started(harness: Harness) -> Harness:
    assert await harness.post(start_update()) == 200
    harness.forget()
    return harness


# --- Онбординг и меню ----------------------------------------------------


async def test_starting_the_bot_greets_and_shows_the_menu(harness: Harness) -> None:
    """Критерий №1 и №2 — теперь во втором мессенджере."""
    assert await harness.post(start_update()) == 200

    sent = harness.bot.sent
    assert len(sent) == 1
    assert harness.texts_said()[0].startswith("Привет!")
    labels = [button.text for row in sent[0].buttons for button in row]
    assert labels == [
        texts.MENU_IMAGES,
        texts.MENU_PRESETS,
        texts.MENU_PROFILE,
        texts.MENU_TARIFFS,
    ]


async def test_the_menu_rides_along_with_every_message(started: Harness) -> None:
    """Постоянных клавиатур в MAX нет — меню прикрепляется к каждому ответу."""
    await started.send_text("привет")

    assert started.bot.sent[-1].buttons, "меню не доехало"


async def test_a_deeplink_gift_reaches_the_invited_user(harness: Harness) -> None:
    """§2.7 в MAX: payload — прямой аналог /start ref_XXXX."""
    inviter = await harness.storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="1000",
        referral_code="friend01",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )

    assert await harness.post(start_update("ref_friend01")) == 200

    invited = await harness.user()
    assert invited.bonus_messages == 50
    refreshed = await harness.storage.get_user_by_id(inviter.id)
    assert refreshed is not None
    assert refreshed.bonus_images == 5


async def test_the_profile_carries_the_support_number(started: Harness) -> None:
    """В MAX это единственный способ опознать написавшего в поддержку."""
    await started.press(Action.MENU_PROFILE)

    user = await started.user()
    assert f"Твой номер: {user.support_number}" in started.texts_said()[0]
    assert 100_000 <= user.support_number <= 999_999, "номер должен быть случайным"


# --- Чат и картинки ------------------------------------------------------


async def test_a_question_is_answered(started: Harness) -> None:
    started.llm.answer = "Париж."

    await started.send_text("какая столица Франции?")

    assert started.texts_said() == ["Париж."]


async def test_a_menu_button_press_opens_its_screen(started: Harness) -> None:
    await started.press(Action.MENU_IMAGES)

    assert started.texts_said() == [texts.IMAGE_ASK]
    assert started.bot.callbacks == ["mid-in-1"], "нажатие не подтверждено"


async def test_the_waiting_message_is_edited_into_the_picture(
    started: Harness,
) -> None:
    """Критерий №5. В MAX это настоящее редактирование, а не пересылка заново."""
    await started.press(Action.MENU_IMAGES)
    started.forget()

    await started.send_text("кот-космонавт")

    assert started.texts_said() == [texts.IMAGE_DRAWING]
    assert len(started.bot.edits) == 1
    assert started.bot.edits[0].images, "картинка не приехала в редактирование"


async def test_the_limit_is_spent_only_after_delivery(started: Harness) -> None:
    """Главный инвариант проекта — на живом пути MAX."""
    started.images.error = RuntimeError("провайдер лёг")

    await started.press(Action.MENU_IMAGES)
    await started.send_text("кот-космонавт")

    user = await started.user()
    usage = await started.storage.get_usage(user.id, _today())
    assert usage.images_used == 0


async def test_sharing_forwards_the_picture_by_token(started: Harness) -> None:
    await started.press(Action.MENU_IMAGES)
    await started.send_text("кот-космонавт")
    started.forget()

    await started.press(Action.IMAGE_SHARE)

    sent = started.bot.sent[-1]
    assert sent.images, "картинка не отправлена"
    assert sent.images[0].payload.token == "token-1"


async def test_the_referral_link_points_at_max_not_telegram(started: Harness) -> None:
    """Ссылка в MAX обязана вести в MAX: телеграмная там просто не откроется."""
    await started.press(Action.MY_LINK)
    started.forget()
    await started.press(Action.REFERRAL_SEND)

    said = started.texts_said()[-1]
    assert said.startswith("Тут бесплатный ChatGPT")
    assert "https://max.ru/testbot?start=ref_" in said


# --- Пресеты -------------------------------------------------------------


async def test_a_preset_applies_to_a_photo(started: Harness) -> None:
    """Критерий №6 во втором мессенджере, включая скачивание по адресу."""
    await started.press(preset_action("lego"))
    started.forget()

    await started.send_photo()

    assert started.images.edited, "фото не ушло провайдеру"
    assert started.bot.edits[-1].images, "результат не заменил ожидание"


# --- Транспорт -----------------------------------------------------------


async def test_a_request_without_the_secret_is_rejected(harness: Harness) -> None:
    """§3.5: подпись вебхука MAX — прямой аналог телеграмной."""
    response = await harness.client.post(PATH, json=start_update())

    assert response.status == 403
    assert harness.bot.sent == []


async def test_a_redelivered_update_is_handled_once(started: Harness) -> None:
    """Сквозного update_id в MAX нет, и повтор ловится составным ключом."""
    update = text_update("mid-repeat", "привет")

    assert await started.post(update) == 200
    assert await started.post(update) == 200

    assert len(started.texts_said()) == 1
