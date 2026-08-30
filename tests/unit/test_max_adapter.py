"""MAX-адаптер: разбор входящего, клавиатуры и отправка.

Ровно то же, что проверяется у Telegram, — и это не совпадение. Если бы для
MAX понадобились другие проверки продуктового поведения, значит продуктовая
логика утекла бы в адаптер, а этого быть не должно (критерий A4).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from maxapi.types import LinkButton

from app.adapters.max import keyboards as max_keyboards
from app.adapters.max.intake import dedup_key
from app.adapters.max.messenger import MaxMessenger
from app.adapters.max.router import to_incoming
from app.core import texts
from app.core.models import Button, Chat, Keyboard, MessengerKind
from app.core.photos import PhotoTooLargeError
from tests.fakes import PNG_BYTES

CHAT_ID = 555
CORE_CHAT = Chat(messenger=MessengerKind.MAX, chat_id=str(CHAT_ID))


# --- Дедупликация --------------------------------------------------------


def test_a_message_is_keyed_by_its_own_id() -> None:
    """Сквозного update_id в MAX нет, ключ собирается из полей события."""
    key = dedup_key(
        {"update_type": "message_created", "message": {"body": {"mid": "mid-1"}}}
    )

    assert key == "max:msg:mid-1"


def test_a_button_press_is_keyed_by_the_callback() -> None:
    key = dedup_key(
        {"update_type": "message_callback", "callback": {"callback_id": "cb-1"}}
    )

    assert key == "max:cb:cb-1"


def test_bot_started_is_keyed_by_chat_and_time() -> None:
    """У запуска бота своего идентификатора нет — ключ составной."""
    event: dict[str, Any] = {
        "update_type": "bot_started",
        "chat_id": 7,
        "timestamp": 1_700_000_000,
    }

    assert dedup_key(event) == "max:start:7:1700000000"
    assert dedup_key(event) == dedup_key(dict(event)), "повтор обязан совпасть"


def test_an_update_we_did_not_subscribe_to_has_no_key() -> None:
    assert dedup_key({"update_type": "message_edited"}) is None


def test_a_message_without_an_id_has_no_key() -> None:
    assert dedup_key({"update_type": "message_created", "message": {}}) is None


# --- Разбор входящего ----------------------------------------------------


def test_bot_started_carries_the_deeplink_payload() -> None:
    """§2.7 в MAX работает: payload — прямой аналог /start ref_XXXX."""
    incoming = to_incoming(
        {
            "update_type": "bot_started",
            "chat_id": CHAT_ID,
            "user": {"user_id": 9},
            "payload": "ref_abc123",
        }
    )

    assert incoming is not None
    assert incoming.start_payload == "ref_abc123"
    assert incoming.chat == CORE_CHAT
    assert incoming.external_user_id == "9"


def test_a_start_without_a_link_is_still_a_start() -> None:
    """Пустая строка и None — разные случаи: одно здоровается, другое нет."""
    incoming = to_incoming(
        {"update_type": "bot_started", "chat_id": CHAT_ID, "user": {"user_id": 9}}
    )

    assert incoming is not None
    assert incoming.start_payload == ""


def test_a_text_message_is_parsed() -> None:
    incoming = to_incoming(_message(text="привет"))

    assert incoming is not None
    assert incoming.text == "привет"
    assert incoming.chat == CORE_CHAT


def test_a_message_from_a_bot_is_ignored() -> None:
    raw = _message(text="привет")
    raw["message"]["sender"]["is_bot"] = True

    assert to_incoming(raw) is None


def test_a_photo_is_taken_by_its_address() -> None:
    """Скачать картинку можно по адресу, а токен для этого не годится."""
    incoming = to_incoming(
        _message(
            attachments=[{"type": "image", "payload": {"url": "https://cdn/photo.jpg"}}]
        )
    )

    assert incoming is not None
    assert incoming.photo_ref == "https://cdn/photo.jpg"


def test_a_sticker_is_not_mistaken_for_a_photo() -> None:
    incoming = to_incoming(
        _message(attachments=[{"type": "sticker", "payload": {"url": "https://s"}}])
    )

    assert incoming is not None
    assert incoming.photo_ref is None


def test_a_button_press_becomes_an_action() -> None:
    incoming = to_incoming(
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb1",
                "payload": "m:me",
                "user": {"user_id": 9},
            },
            "message": {"recipient": {"chat_id": CHAT_ID}},
        }
    )

    assert incoming is not None
    assert incoming.action == "m:me"
    assert incoming.callback_id == "cb1"
    assert incoming.chat == CORE_CHAT


def test_a_press_on_a_lost_message_still_reaches_the_right_chat() -> None:
    """В личном чате идентификатор чата совпадает с пользователем."""
    incoming = to_incoming(
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb1",
                "payload": "m:me",
                "user": {"user_id": 9},
            },
        }
    )

    assert incoming is not None
    assert incoming.chat.chat_id == "9"


def test_an_unsubscribed_update_is_ignored() -> None:
    assert to_incoming({"update_type": "dialog_cleared"}) is None


# --- Клавиатуры ----------------------------------------------------------


def test_the_menu_is_attached_to_every_message() -> None:
    """Постоянных клавиатур в MAX нет, поэтому меню едет с каждым сообщением."""
    attachment = max_keyboards.build(None, show_menu=True)

    assert attachment is not None
    labels = [button.text for row in attachment.payload.buttons for button in row]
    assert labels == [
        texts.MENU_IMAGES,
        texts.MENU_PRESETS,
        texts.MENU_PROFILE,
        texts.MENU_TARIFFS,
    ]


def test_screen_buttons_and_the_menu_live_together() -> None:
    """В MAX клавиатура одна, но рядов в ней сколько угодно.

    В Telegram кнопки экрана вытесняют меню — там это разные механизмы.
    Здесь обе группы помещаются в одно вложение, и человеку доступно всё.
    """
    attachment = max_keyboards.build(
        Keyboard.row(Button(text="Повторить", action="c:retry")), show_menu=True
    )

    assert attachment is not None
    assert len(attachment.payload.buttons) == 3
    assert attachment.payload.buttons[0][0].text == "Повторить"


def test_a_message_can_ask_for_no_buttons_at_all() -> None:
    assert max_keyboards.build(None, show_menu=False) is None


def test_a_link_button_becomes_a_link() -> None:
    attachment = max_keyboards.build(
        Keyboard.row(Button(text="Открыть", url="https://example.com")),
        show_menu=False,
    )

    assert attachment is not None
    button = attachment.payload.buttons[0][0]
    assert isinstance(button, LinkButton)
    assert button.url == "https://example.com"


# --- Скачивание фото -----------------------------------------------------


def _messenger(handler: Any) -> MaxMessenger:
    transport = httpx.MockTransport(handler)
    return MaxMessenger(bot=None, http=httpx.AsyncClient(transport=transport))  # type: ignore[arg-type]


async def test_a_photo_is_downloaded() -> None:
    messenger = _messenger(
        lambda request: httpx.Response(
            200, content=PNG_BYTES, headers={"content-type": "image/png"}
        )
    )

    photo = await messenger.download_photo("https://cdn/p.jpg", max_bytes=1024)

    assert photo.data == PNG_BYTES
    assert photo.mime_type == "image/png"


async def test_an_oversized_photo_is_refused_before_reading() -> None:
    """Заявленный размер бесплатен: проверять его надо первым (§3.5)."""
    messenger = _messenger(
        lambda request: httpx.Response(
            200, content=b"x" * 10, headers={"content-length": "999999"}
        )
    )

    with pytest.raises(PhotoTooLargeError):
        await messenger.download_photo("https://cdn/p.jpg", max_bytes=1024)


async def test_a_lying_size_does_not_get_past_the_limit() -> None:
    """Заголовок приходит снаружи — на него нельзя полагаться."""
    messenger = _messenger(
        lambda request: httpx.Response(200, content=b"x" * 8192, headers={})
    )

    with pytest.raises(PhotoTooLargeError):
        await messenger.download_photo("https://cdn/p.jpg", max_bytes=1024)


def _message(
    *, text: str | None = None, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 9, "is_bot": False},
            "recipient": {"chat_id": CHAT_ID, "chat_type": "dialog"},
            "timestamp": 1,
            "body": {
                "mid": "mid-1",
                "seq": 1,
                "text": text,
                "attachments": attachments,
            },
        },
    }


def test_the_delivered_photo_reference_is_a_token() -> None:
    """«Поделиться» пересылает по токену, а не заливает байты заново."""
    from app.adapters.max.messenger import _by_token

    attachment = _by_token("tok-1")

    assert attachment.payload.token == "tok-1"
