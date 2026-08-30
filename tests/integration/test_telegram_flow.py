"""Сквозная проверка Telegram-адаптера.

Обновление проходит весь путь целиком: HTTP-запрос вебхука → очередь →
диспетчер aiogram → адаптер → общий маршрутизатор → сценарий → вызов Bot API.
Сеть не используется — подменена только сессия Telegram, всё остальное
настоящее, включая хранилище, ограничитель одновременных задач и клавиатуры.

Смысл именно в этом: юнит-тесты сценариев проверяют решения, а здесь
проверяется проводка. Ошибка вроде «забыли callback_query в allowed_updates»
или «кнопка присылает подпись, а её никто не разбирает» видна только так.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    DeleteMessage,
    EditMessageText,
    GetFile,
    SendInvoice,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    Chat,
    File,
    InlineKeyboardMarkup,
    Invoice,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
)
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.telegram import router as telegram_router
from app.adapters.telegram.messenger import TelegramMessenger
from app.adapters.telegram.stars import TelegramStars
from app.core import texts
from app.core.actions import Action, buy_action, method_action, preset_action
from app.core.models import MessengerKind, TariffId
from app.core.scenarios.deps import Deps
from app.core.settings import CoreSettings
from app.infra.antiflood import FloodGuard
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import TELEGRAM_SECRET_HEADER, Webhook, create_app
from app.ports.payments import PaymentMethod
from tests.fakes import (
    PNG_BYTES,
    FakeCards,
    FakeImages,
    FakeLLM,
    FakeLogger,
    FrozenClock,
)

SECRET = "integration-webhook-secret"
PATH = "/webhook/telegram"
CHAT_ID = 555
WebClient = TestClient[web.Request, web.Application]


class RecordingSession(BaseSession):
    """Сессия Telegram, которая ничего не отправляет, но всё запоминает."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        #: Что отдаётся при скачивании файла.
        self.file_content = PNG_BYTES
        #: Размер, который Telegram сообщает про файл.
        self.file_size: int | None = len(PNG_BYTES)
        self._next_message_id = 1000

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура из aiogram
    ) -> Any:
        self.calls.append(method)
        self._next_message_id += 1

        if isinstance(method, SendMessage):
            return Message(
                message_id=self._next_message_id,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                text=method.text,
            ).as_(bot)

        if isinstance(method, SendPhoto):
            return Message(
                message_id=self._next_message_id,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                caption=method.caption,
                photo=[
                    PhotoSize(
                        file_id="delivered-small",
                        file_unique_id="u1",
                        width=64,
                        height=64,
                    ),
                    PhotoSize(
                        file_id="delivered-large",
                        file_unique_id="u2",
                        width=1024,
                        height=1024,
                    ),
                ],
            ).as_(bot)

        if isinstance(method, SendInvoice):
            return Message(
                message_id=self._next_message_id,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                invoice=Invoice(
                    title=method.title,
                    description=method.description,
                    start_parameter="",
                    currency=method.currency,
                    total_amount=method.prices[0].amount,
                ),
            ).as_(bot)

        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id,
                file_unique_id="unique",
                file_size=self.file_size,
                file_path="photos/file_1.jpg",
            )

        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура из aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield self.file_content

    async def close(self) -> None:
        return None


# --- Сборка обновлений ---------------------------------------------------


def _message_update(update_id: int, **message: Any) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Тест"},
            **message,
        },
    }


def text_update(update_id: int, text: str) -> dict[str, Any]:
    return _message_update(update_id, text=text)


def photo_update(update_id: int, file_id: str = "incoming-photo") -> dict[str, Any]:
    return _message_update(
        update_id,
        photo=[
            {
                "file_id": file_id,
                "file_unique_id": "in1",
                "width": 1024,
                "height": 1024,
                "file_size": len(PNG_BYTES),
            }
        ],
    )


def pre_checkout_update(update_id: int, order_id: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "pre_checkout_query": {
            "id": f"pre{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Тест"},
            "currency": "XTR",
            "total_amount": 524,
            "invoice_payload": order_id,
        },
    }


def paid_update(update_id: int, order_id: str) -> dict[str, Any]:
    return _message_update(
        update_id,
        successful_payment={
            "currency": "XTR",
            "total_amount": 524,
            "invoice_payload": order_id,
            "telegram_payment_charge_id": "charge-1",
            "provider_payment_charge_id": "",
        },
    )


def callback_update(update_id: int, data: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Тест"},
            "chat_instance": "instance",
            "data": data,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "предыдущее сообщение",
            },
        },
    }


@dataclass
class Harness:
    """Поднятое приложение вместе с подменённой сессией Telegram."""

    client: WebClient
    session: RecordingSession
    queue: JobQueue[dict[str, Any]]
    storage: InMemoryStorage
    llm: FakeLLM
    images: FakeImages
    clock: FrozenClock
    logger: FakeLogger
    _update_id: list[int] = field(default_factory=lambda: [0])

    def next_id(self) -> int:
        self._update_id[0] += 1
        return self._update_id[0]

    async def post(self, update: dict[str, Any]) -> int:
        """Шлёт обновление с правильным секретом и ждёт его обработки."""
        response = await self.client.post(
            PATH, json=update, headers={TELEGRAM_SECRET_HEADER: SECRET}
        )
        await self.settle()
        return response.status

    async def send_text(self, text: str) -> None:
        assert await self.post(text_update(self.next_id(), text)) == 200

    async def send_photo(self, file_id: str = "incoming-photo") -> None:
        assert await self.post(photo_update(self.next_id(), file_id)) == 200

    async def press(self, action: str) -> None:
        assert await self.post(callback_update(self.next_id(), action)) == 200

    async def user(self) -> Any:
        """Пользователь этого чата — через порт, а не через внутренности."""
        found = await self.storage.get_user(MessengerKind.TELEGRAM, str(CHAT_ID))
        assert found is not None
        return found

    async def settle(self) -> None:
        """Ждёт, пока очередь опустеет, не останавливая воркеров."""
        assert await self.queue.join(timeout=5.0)

    def messages(self) -> list[SendMessage]:
        return list(self.calls_of(SendMessage))

    def texts_said(self) -> list[str]:
        return [message.text for message in self.messages()]

    def photos(self) -> list[SendPhoto]:
        return list(self.calls_of(SendPhoto))

    def calls_of(self, kind: type) -> list[Any]:
        return [call for call in self.session.calls if isinstance(call, kind)]

    def forget(self) -> None:
        """Забывает записанные вызовы — чтобы проверять следующий шаг."""
        self.session.calls.clear()


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    from app.adapters.telegram.intake import dedup_key
    from app.main import build_intake

    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)

    clock = FrozenClock()
    storage = InMemoryStorage(clock=clock)
    cards = FakeCards()
    # Звёзды — настоящим адаптером: весь смысл этого файла в том, чтобы
    # проверить проводку, а фейк проверил бы сам себя.
    stars = TelegramStars(bot)
    llm = FakeLLM()
    images = FakeImages()
    logger = FakeLogger()

    deps = Deps(
        storage=storage,
        messenger=TelegramMessenger(bot),
        llm=llm,
        images=images,
        settings=CoreSettings(bot_username="testbot"),
        logger=logger,
        guard=FloodGuard(limit=1),
        cards=cards,
        stars=stars,
        now=clock,
    )
    dispatcher = telegram_router.build_dispatcher(deps)

    async def handle_update(raw: dict[str, Any]) -> None:
        await dispatcher.feed_raw_update(bot, raw)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "test-updates", handle_update, capacity=50, workers=2
    )
    queue.start()
    dedup = Deduplicator(ttl_seconds=600, max_keys=1000)

    app = create_app(
        webhooks=[
            Webhook(
                messenger="telegram",
                path=PATH,
                secret_header=TELEGRAM_SECRET_HEADER,
                secret=SECRET,
                submit=build_intake(
                    queue, dedup, messenger="telegram", key_of=dedup_key
                ),
            ),
        ],
        health=lambda: {"status": "ok"},
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(
        client=client,
        session=session,
        queue=queue,
        storage=storage,
        llm=llm,
        images=images,
        clock=clock,
        logger=logger,
    )

    await queue.drain(timeout=2.0)
    await client.close()


@pytest.fixture
async def started(harness: Harness) -> Harness:
    """Пользователь, который уже нажал /start."""
    await harness.send_text("/start")
    harness.forget()
    return harness


# --- Онбординг -----------------------------------------------------------


async def test_start_greets_and_shows_the_menu(harness: Harness) -> None:
    """Критерий №1: первый экран, и критерий №2: постоянное меню."""
    await harness.send_text("/start")

    sent = harness.messages()
    assert len(sent) == 1
    assert texts._GREETING in sent[0].text
    assert isinstance(sent[0].reply_markup, ReplyKeyboardMarkup)
    labels = [button.text for row in sent[0].reply_markup.keyboard for button in row]
    assert labels == [
        texts.MENU_IMAGES,
        texts.MENU_PRESETS,
        texts.MENU_PROFILE,
        texts.MENU_TARIFFS,
    ]


async def test_start_creates_the_user_once(harness: Harness) -> None:
    """Повторный /start не заводит второго пользователя и не дарит бонусов."""
    await harness.send_text("/start")
    await harness.send_text("/start")

    user = await harness.user()
    assert user.bonus_images == 0
    assert await harness.storage.get_user(MessengerKind.TELEGRAM, "999") is None


async def test_message_without_start_still_gets_an_answer(harness: Harness) -> None:
    """Человек может написать, не нажав /start: молчать в ответ нельзя."""
    await harness.send_text("привет")

    assert harness.texts_said()[0].startswith(texts._GREETING)
    assert await harness.user() is not None


# --- Чат -----------------------------------------------------------------


async def test_plain_text_gets_an_answer_from_the_provider(started: Harness) -> None:
    started.llm.answer = "Столица Франции — Париж."

    await started.send_text("какая столица Франции?")

    assert started.texts_said() == ["Столица Франции — Париж."]
    assert started.llm.calls[0][0][-1].content == "какая столица Франции?"


async def test_the_start_command_is_never_treated_as_a_question(
    harness: Harness,
) -> None:
    """/start не должен уехать в чат вопросом и стоить человеку сообщения."""
    await harness.send_text("/start")

    assert harness.llm.calls == []


# --- Меню ----------------------------------------------------------------


async def test_menu_button_arrives_as_text_and_still_works(started: Harness) -> None:
    """Постоянная клавиатура присылает подпись — её надо узнать в тексте.

    Если бы подпись не разбиралась, «🎨 Картинки» уехало бы в чат вопросом:
    человек нажал кнопку, а бот ответил бы на неё как на реплику.
    """
    await started.send_text(texts.MENU_IMAGES)

    assert started.texts_said() == [texts.IMAGE_ASK]
    assert started.llm.calls == []


async def test_profile_opens_by_callback(started: Harness) -> None:
    """Критерий №8 и заодно проверка, что нажатия вообще доходят."""
    await started.press(Action.MENU_PROFILE)

    said = started.texts_said()[0]
    assert texts.TARIFF_TITLES[(await started.user()).tariff] in said
    assert "Сообщений сегодня: 0 из 20" in said
    # Индикатор на кнопке гасится до работы, а не после.
    assert started.calls_of(AnswerCallbackQuery)


async def test_tariffs_arrive_as_one_message_with_three_buttons(
    started: Harness,
) -> None:
    await started.press(Action.MENU_TARIFFS)

    sent = started.messages()
    assert len(sent) == 1
    markup = sent[0].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [button.text for button in markup.inline_keyboard[0]] == [
        "Лайт",
        "Про",
        "Макс",
    ]


async def test_my_link_offers_a_button_that_sends_the_invitation(
    started: Harness,
) -> None:
    """Два шага: сначала за что, потом само приглашение одной кнопкой."""
    await started.press(Action.MY_LINK)
    assert "+50 сообщений" in started.texts_said()[0]
    started.forget()

    await started.press(Action.REFERRAL_SEND)

    said = started.texts_said()[0]
    assert said.startswith("Тут бесплатный ChatGPT")
    assert (await started.user()).referral_code in said


# --- Картинки ------------------------------------------------------------


async def test_drawing_replaces_the_waiting_message(started: Harness) -> None:
    """Критерий №5: «Рисую…» не остаётся в переписке."""
    await started.send_text(texts.MENU_IMAGES)
    started.forget()

    await started.send_text("кот-космонавт")

    assert started.texts_said() == [texts.IMAGE_DRAWING]
    assert started.calls_of(DeleteMessage), "сообщение ожидания осталось висеть"
    photos = started.photos()
    assert len(photos) == 1
    assert isinstance(photos[0].reply_markup, InlineKeyboardMarkup)


async def test_the_image_limit_is_spent_only_after_delivery(started: Harness) -> None:
    """Главный инвариант — на живом пути, а не только в юнит-тесте."""
    started.images.error = RuntimeError("провайдер лёг")

    await started.send_text(texts.MENU_IMAGES)
    await started.send_text("кот-космонавт")

    usage = await started.storage.get_usage((await started.user()).id, _today())
    assert usage.images_used == 0
    assert texts.IMAGE_ERROR in [
        call.text for call in started.calls_of(EditMessageText)
    ]


async def test_after_drawing_the_next_message_goes_back_to_chat(
    started: Harness,
) -> None:
    """Режим «жду описание» одноразовый: иначе из него не выйти."""
    await started.send_text(texts.MENU_IMAGES)
    await started.send_text("кот-космонавт")
    started.forget()

    await started.send_text("а сколько будет два плюс два?")

    assert started.llm.calls
    assert started.texts_said() == [started.llm.answer]


async def test_share_sends_the_delivered_photo_with_a_referral_link(
    started: Harness,
) -> None:
    """Критерий №5: под картинкой персональная ссылка, а не общая."""
    await started.send_text(texts.MENU_IMAGES)
    await started.send_text("кот-космонавт")
    started.forget()

    await started.press(Action.IMAGE_SHARE)

    photos = started.photos()
    assert len(photos) == 1
    # Картинка пересылается по идентификатору, а не заливается заново.
    assert photos[0].photo == "delivered-large"
    assert (await started.user()).referral_code in (photos[0].caption or "")


# --- Пресеты -------------------------------------------------------------


async def test_preset_applies_to_the_photo_in_one_step(started: Harness) -> None:
    """Критерий №6: выбрал прикол, кинул фото — получил результат."""
    await started.press(preset_action("lego"))
    assert started.texts_said() == ["Кинь фото — сделаю из тебя лего"]
    started.forget()

    await started.send_photo()

    assert started.texts_said() == [texts.PRESET_WORKING]
    assert started.photos(), "результат не отправлен"
    assert started.images.edited, "фото не ушло провайдеру"


async def test_a_photo_without_a_preset_offers_the_menu(started: Harness) -> None:
    """Фото «просто так» не должно проваливаться в тишину."""
    await started.send_photo()

    assert started.texts_said() == [texts.PRESETS_ASK]
    assert started.images.edited == []


async def test_an_oversized_photo_costs_nothing(started: Harness) -> None:
    """Отказ по размеру — до провайдера и до списания (§3.5)."""
    started.session.file_size = 50 * 1024 * 1024

    await started.press(preset_action("lego"))
    started.forget()
    await started.send_photo()

    assert started.texts_said() == [texts.PHOTO_TOO_BIG]
    assert started.images.edited == []


# --- Прочее --------------------------------------------------------------


async def test_unauthorized_update_never_reaches_the_bot(harness: Harness) -> None:
    """Чужой запрос не должен доходить до бота вообще."""
    response = await harness.client.post(PATH, json=text_update(90, "привет"))
    await harness.settle()

    assert response.status == 403
    assert harness.session.calls == []


async def test_redelivered_update_is_answered_once(started: Harness) -> None:
    """Повторная доставка не должна стоить второго ответа и второго лимита."""
    update = text_update(500, "привет")

    assert await started.post(update) == 200
    assert await started.post(update) == 200

    assert len(started.messages()) == 1


async def test_unsupported_content_gets_an_answer(started: Harness) -> None:
    """Стикер — не повод молчать."""
    await started.post(
        _message_update(
            600,
            sticker={
                "file_id": "s1",
                "file_unique_id": "s1u",
                "width": 512,
                "height": 512,
                "is_animated": False,
                "is_video": False,
                "type": "regular",
            },
        )
    )

    assert started.texts_said() == [texts.UNSUPPORTED_INPUT]


async def test_a_failing_scenario_still_answers_the_user(started: Harness) -> None:
    """Неожиданный сбой не должен оставлять человека в тишине."""
    started.llm.error = RuntimeError("совсем неожиданно")
    started.session.file_size = None

    await started.send_text("привет")

    # Сценарий чата сам показывает свою ошибку — это не последний рубеж,
    # а штатная обработка. Проверяем, что ответ есть и он про повтор.
    assert texts.CHAT_ERROR in started.texts_said()


def _today() -> Any:
    from app.core.limits import current_day

    return current_day(FrozenClock().now, "Europe/Moscow")


# --- Оплата звёздами -----------------------------------------------------


async def test_paying_with_stars_turns_the_tariff_on(started: Harness) -> None:
    """Весь путь оплаты целиком, от кнопки до включённого тарифа.

    Юнит-тесты проверяют решения, а здесь проверяется проводка: дошёл ли
    pre_checkout_query до бота (без него Telegram платёж не проведёт), опознан
    ли заказ по payload, выдан ли тариф ровно один раз.
    """
    await started.press(buy_action(TariffId.PRO.value))
    assert "Чем платим?" in started.texts_said()[0]
    started.forget()

    await started.press(method_action(PaymentMethod.STARS.value, TariffId.PRO.value))

    invoices = started.calls_of(SendInvoice)
    assert len(invoices) == 1
    assert invoices[0].currency == "XTR"
    order_id = invoices[0].payload
    started.forget()

    # Telegram спрашивает перед списанием и ждёт ответа считаные секунды.
    assert await started.post(pre_checkout_update(started.next_id(), order_id)) == 200
    approvals = started.calls_of(AnswerPreCheckoutQuery)
    assert len(approvals) == 1
    assert approvals[0].ok is True
    started.forget()

    assert await started.post(paid_update(started.next_id(), order_id)) == 200

    user = await started.user()
    assert user.tariff is TariffId.PRO
    assert user.tariff_expires_at is not None
    assert "Про" in started.texts_said()[-1]


async def test_an_unknown_invoice_is_refused_before_the_money_moves(
    started: Harness,
) -> None:
    """Согласиться на платёж, который нечем закрыть, дороже отказа."""
    assert (
        await started.post(pre_checkout_update(started.next_id(), "выдуманный заказ"))
        == 200
    )

    approvals = started.calls_of(AnswerPreCheckoutQuery)
    assert len(approvals) == 1
    assert approvals[0].ok is False


async def test_a_repeated_payment_notice_does_not_extend_the_subscription(
    started: Harness,
) -> None:
    """Одна оплата — один месяц, сколько бы уведомлений ни пришло."""
    await started.press(method_action(PaymentMethod.STARS.value, TariffId.PRO.value))
    order_id = started.calls_of(SendInvoice)[0].payload

    await started.post(paid_update(started.next_id(), order_id))
    first = (await started.user()).tariff_expires_at

    await started.post(paid_update(started.next_id(), order_id))
    second = (await started.user()).tariff_expires_at

    assert first == second
