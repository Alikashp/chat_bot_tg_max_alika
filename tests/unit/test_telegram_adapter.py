"""Telegram-адаптер: разбор входящего и отправка исходящего.

Здесь проверяется именно перевод между Telegram и общим видом. Продуктовые
решения к этому файлу отношения не имеют — они в tests/unit/test_router.py.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    User,
)

from app.adapters.telegram import keyboards as tg_keyboards
from app.adapters.telegram.messenger import TelegramMessenger
from app.adapters.telegram.router import callback_to_incoming, to_incoming
from app.core import texts
from app.core.models import Button, Keyboard, MessageRef, MessengerKind, Photo
from app.core.models import Chat as CoreChat
from app.core.photos import PhotoTooLargeError
from tests.fakes import PNG_BYTES

CHAT_ID = 555
CORE_CHAT = CoreChat(messenger=MessengerKind.TELEGRAM, chat_id=str(CHAT_ID))


# --- Разбор входящего ----------------------------------------------------


def _message(**fields: Any) -> Message:
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": datetime.now(UTC),
        "chat": Chat(id=CHAT_ID, type="private"),
        "from_user": User(id=CHAT_ID, is_bot=False, first_name="Тест"),
    }
    payload.update(fields)
    return Message(**payload)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", ""),
        ("/start ref_abc123", "ref_abc123"),
        ("/start@mybot pres_1", "pres_1"),
        ("/startover", None),
        ("привет", None),
    ],
)
def test_start_payload_is_parsed(text: str, expected: str | None) -> None:
    """Пустая строка и None — разные ответы: одно здоровается, другое нет."""
    incoming = to_incoming(_message(text=text))

    assert incoming is not None
    assert incoming.start_payload == expected


def test_the_start_command_does_not_leak_into_the_text() -> None:
    """Иначе «/start» уехало бы в чат вопросом и стоило бы сообщения."""
    incoming = to_incoming(_message(text="/start ref_abc"))

    assert incoming is not None
    assert incoming.text is None


def test_a_group_message_is_ignored() -> None:
    """Бот личный: отвечать на каждую реплику в чужой беседе он не должен."""
    assert to_incoming(_message(chat=Chat(id=-1, type="group"), text="привет")) is None


def test_a_message_from_another_bot_is_ignored() -> None:
    other = User(id=7, is_bot=True, first_name="Бот")

    assert to_incoming(_message(from_user=other, text="привет")) is None


def test_the_largest_photo_size_is_taken() -> None:
    """Мелкий превью-вариант обработать можно, но результат будет мыльным."""
    incoming = to_incoming(
        _message(
            photo=[
                PhotoSize(file_id="small", file_unique_id="a", width=90, height=90),
                PhotoSize(file_id="big", file_unique_id="b", width=1280, height=1280),
            ]
        )
    )

    assert incoming is not None
    assert incoming.photo_file_id == "big"


def test_a_caption_counts_as_text() -> None:
    incoming = to_incoming(_message(caption="подпись к фото"))

    assert incoming is not None
    assert incoming.text == "подпись к фото"


def _callback(**fields: Any) -> CallbackQuery:
    payload: dict[str, Any] = {
        "id": "cb1",
        "from_user": User(id=CHAT_ID, is_bot=False, first_name="Тест"),
        "chat_instance": "instance",
        "data": "m:me",
    }
    payload.update(fields)
    return CallbackQuery(**payload)


def test_a_callback_becomes_an_action() -> None:
    incoming = callback_to_incoming(_callback(message=_message()))

    assert incoming is not None
    assert incoming.action == "m:me"
    assert incoming.callback_id == "cb1"
    assert incoming.chat.chat_id == str(CHAT_ID)


def test_a_callback_without_data_is_ignored() -> None:
    assert callback_to_incoming(_callback(data=None)) is None


def test_a_callback_on_a_lost_message_still_works() -> None:
    """Сообщение под кнопкой могло стать боту недоступно, человек — нет."""
    incoming = callback_to_incoming(_callback(message=None))

    assert incoming is not None
    assert incoming.chat.chat_id == str(CHAT_ID)


# --- Клавиатуры ----------------------------------------------------------


def test_the_menu_is_a_persistent_reply_keyboard() -> None:
    """Критерий №2: меню доступно с любого экрана, а не под одним сообщением."""
    menu = tg_keyboards.main_menu()

    assert isinstance(menu, ReplyKeyboardMarkup)
    assert menu.is_persistent is True
    labels = [button.text for row in menu.keyboard for button in row]
    assert labels == [
        texts.MENU_IMAGES,
        texts.MENU_PRESETS,
        texts.MENU_PROFILE,
        texts.MENU_TARIFFS,
    ]


def test_an_action_button_becomes_callback_data() -> None:
    markup = tg_keyboards.inline(Keyboard.row(Button(text="Жми", action="m:me")))

    assert markup.inline_keyboard[0][0].callback_data == "m:me"


def test_a_link_button_becomes_a_url() -> None:
    markup = tg_keyboards.inline(
        Keyboard.row(Button(text="Открыть", url="https://example.com"))
    )

    button = markup.inline_keyboard[0][0]
    assert button.url == "https://example.com"
    assert button.callback_data is None


# --- Отправка ------------------------------------------------------------


class StubSession(BaseSession):
    """Сессия, которая записывает вызовы и отдаёт заготовленные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.file_size: int | None = len(PNG_BYTES)
        self.chunks: list[bytes] = [PNG_BYTES]
        self.delete_fails = False
        self.stream_closed = False

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура из aiogram
    ) -> Any:
        self.calls.append(method)
        if isinstance(method, DeleteMessage) and self.delete_fails:
            raise TelegramBadRequest(method=method, message="message can't be deleted")
        if isinstance(method, SendMessage):
            return Message(
                message_id=10,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                text=method.text,
            ).as_(bot)
        if isinstance(method, SendPhoto):
            return Message(
                message_id=11,
                date=datetime.now(UTC),
                chat=Chat(id=CHAT_ID, type="private"),
                photo=[
                    PhotoSize(file_id="small", file_unique_id="a", width=90, height=90),
                    PhotoSize(file_id="big", file_unique_id="b", width=999, height=999),
                ],
            ).as_(bot)
        return File(
            file_id="f",
            file_unique_id="u",
            file_size=self.file_size,
            file_path="photos/file_1.jpg",
        )

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура из aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.stream_closed = True

    async def close(self) -> None:
        return None


def _markup_of(session: StubSession) -> Any:
    """Клавиатура первого отправленного сообщения."""
    sent = session.calls[0]
    assert isinstance(sent, SendMessage)
    return sent.reply_markup


@pytest.fixture
def session() -> StubSession:
    return StubSession()


@pytest.fixture
def messenger(session: StubSession) -> TelegramMessenger:
    return TelegramMessenger(Bot(token="42:TEST", session=session))


async def test_a_plain_message_carries_the_menu(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    await messenger.send_text(CORE_CHAT, "привет")

    assert isinstance(_markup_of(session), ReplyKeyboardMarkup)


async def test_buttons_under_a_message_win_over_the_menu(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    """У сообщения может быть только одна клавиатура, и это не наш выбор.

    Меню при этом не пропадает: reply-клавиатура остаётся на экране с
    предыдущего сообщения.
    """
    await messenger.send_text(
        CORE_CHAT, "привет", keyboard=Keyboard.row(Button(text="Жми", action="m:me"))
    )

    assert isinstance(_markup_of(session), InlineKeyboardMarkup)


async def test_a_message_can_ask_for_no_keyboard_at_all(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    await messenger.send_text(CORE_CHAT, "Рисую…", show_menu=False)

    assert _markup_of(session) is None


async def test_the_waiting_message_is_removed_before_the_photo(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    """Критерий №5: «Рисую…» не остаётся в переписке мусором."""
    ref = MessageRef(chat=CORE_CHAT, message_id="10")

    delivered = await messenger.edit_to_photo(ref, Photo(data=PNG_BYTES))

    assert isinstance(session.calls[0], DeleteMessage)
    assert isinstance(session.calls[1], SendPhoto)
    assert delivered == "big"


async def test_a_failed_deletion_does_not_swallow_the_result(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    """Остаться без картинки хуже, чем увидеть над ней лишнюю строчку."""
    session.delete_fails = True
    ref = MessageRef(chat=CORE_CHAT, message_id="10")

    delivered = await messenger.edit_to_photo(ref, Photo(data=PNG_BYTES))

    assert delivered == "big"


# --- Скачивание фото -----------------------------------------------------


async def test_a_photo_is_downloaded(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    photo = await messenger.download_photo("f", max_bytes=1024 * 1024)

    assert photo.data == PNG_BYTES
    assert photo.mime_type == "image/jpeg"


async def test_an_oversized_photo_is_refused_before_downloading(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    """Заявленный размер бесплатен: проверить его надо первым (§3.5)."""
    session.file_size = 10 * 1024 * 1024

    with pytest.raises(PhotoTooLargeError):
        await messenger.download_photo("f", max_bytes=1024)

    assert session.stream_closed is False, "качать не начинали"


async def test_a_lying_size_does_not_get_past_the_limit(
    messenger: TelegramMessenger, session: StubSession
) -> None:
    """Заявленный размер приходит снаружи: на него нельзя полагаться."""
    session.file_size = 10
    session.chunks = [b"x" * 4096] * 8

    with pytest.raises(PhotoTooLargeError):
        await messenger.download_photo("f", max_bytes=8192)

    assert session.stream_closed is True, "чтение не оборвали"
