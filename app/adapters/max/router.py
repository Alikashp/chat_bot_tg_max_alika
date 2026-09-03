"""Приведение обновлений MAX к общему виду и передача их в ядро.

Тот же контракт, что у Telegram-адаптера: разобрать своё событие в
IncomingMessage и позвать app/core/router.py. Всё, что происходит дальше —
какие экраны показывать, что считать описанием картинки, когда здороваться, —
общее для обоих мессенджеров и в этом файле не повторяется.

Это и есть проверка A4: продуктовых решений здесь нет ни одного.
"""

from __future__ import annotations

from typing import Any

from app.adapters.max.intake import BOT_STARTED, MESSAGE_CALLBACK, MESSAGE_CREATED
from app.core import router, texts
from app.core.models import Chat, IncomingMessage, MessengerKind
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: Тип вложения с картинкой.
_IMAGE = "image"


async def handle_update(deps: Any, raw_update: dict[str, Any]) -> None:
    """Обрабатывает одно обновление MAX.

    deps типизирован как Any намеренно: иначе адаптер тянул бы за собой
    импорт Deps ради одной аннотации, а собирает его main.
    """
    incoming = to_incoming(raw_update)
    if incoming is None:
        return
    await _dispatch(deps, incoming)


async def _dispatch(deps: Any, incoming: IncomingMessage) -> None:
    """Зовёт ядро и не даёт неожиданному сбою оставить человека без ответа."""
    try:
        await router.handle(deps, incoming)
    except Exception as error:
        logger.error(
            "update_failed",
            messenger="max",
            user_id=_as_int(incoming.external_user_id),
            error=repr(error),
        )
        await _apologise(deps, incoming)


async def _apologise(deps: Any, incoming: IncomingMessage) -> None:
    """Молчание после сбоя выглядит как сломанный бот, и человек уходит."""
    try:
        await deps.messenger.send_text(
            incoming.chat, texts.internal_error().text, show_menu=True
        )
    except Exception as error:
        logger.error("apology_failed", messenger="max", error=repr(error))


def to_incoming(raw_update: dict[str, Any]) -> IncomingMessage | None:
    """Переводит обновление MAX в общий вид; None — если оно не для нас."""
    update_type = raw_update.get("update_type")
    match update_type:
        case _ if update_type == BOT_STARTED:
            return _from_bot_started(raw_update)
        case _ if update_type == MESSAGE_CREATED:
            return _from_message(raw_update)
        case _ if update_type == MESSAGE_CALLBACK:
            return _from_callback(raw_update)
        case _:
            return None


def _username(raw: dict[str, Any], where: str) -> str | None:
    """Имя пользователя из события; None — MAX его не назвал.

    В MAX username есть не у всех, и отсутствие поля здесь — обычное дело,
    а не сбой разбора.
    """
    name = _dig(raw, where, "username")
    return name if isinstance(name, str) and name else None


def _from_bot_started(raw: dict[str, Any]) -> IncomingMessage | None:
    """Запуск бота. Прямой аналог /start с payload (docs/research.md §1.5)."""
    chat_id = raw.get("chat_id")
    user_id = _dig(raw, "user", "user_id")
    if chat_id is None or user_id is None:
        return None

    # Пустая строка, а не None: для ядра «нажал /start без ссылки» и «это не
    # /start» — разные случаи, и путать их нельзя.
    payload = raw.get("payload")
    return IncomingMessage(
        chat=_chat(chat_id),
        external_user_id=str(user_id),
        username=_username(raw, "user"),
        start_payload=payload if isinstance(payload, str) else "",
    )


def _from_message(raw: dict[str, Any]) -> IncomingMessage | None:
    """Обычное сообщение: текст или фото."""
    message = raw.get("message")
    if not isinstance(message, dict):
        return None

    chat_id = _dig(message, "recipient", "chat_id")
    user_id = _dig(message, "sender", "user_id")
    if chat_id is None or user_id is None:
        return None
    if _dig(message, "sender", "is_bot") is True:
        return None

    body = message.get("body")
    body = body if isinstance(body, dict) else {}
    text = body.get("text")
    text = text if isinstance(text, str) and text else None

    return IncomingMessage(
        chat=_chat(chat_id),
        external_user_id=str(user_id),
        username=_username(message, "sender"),
        text=text,
        photo_ref=_photo_url(body.get("attachments")),
    )


def _from_callback(raw: dict[str, Any]) -> IncomingMessage | None:
    """Нажатие inline-кнопки."""
    callback = raw.get("callback")
    if not isinstance(callback, dict):
        return None

    payload = callback.get("payload")
    callback_id = callback.get("callback_id")
    user_id = _dig(callback, "user", "user_id")
    if not isinstance(payload, str) or not payload or user_id is None:
        return None

    # Личный чат: сообщение под кнопкой может быть недоступно, а отвечать
    # всё равно надо. Идентификатор чата берём из сообщения, если он есть,
    # и из пользователя, если нет.
    chat_id = _dig(raw, "message", "recipient", "chat_id")
    if chat_id is None:
        chat_id = _dig(raw, "chat", "chat_id")

    return IncomingMessage(
        chat=_chat(chat_id if chat_id is not None else user_id),
        external_user_id=str(user_id),
        username=_username(callback, "user"),
        action=payload,
        callback_id=str(callback_id) if callback_id else None,
    )


def _photo_url(attachments: Any) -> str | None:
    """Адрес присланной картинки.

    Именно адрес, а не токен: по нему картинку можно скачать, а токен для
    этого не годится (docs/research.md §1.7). Берём первую картинку — MAX
    присылает вложения списком, и разбирать альбом мы не обещали.
    """
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != _IMAGE:
            continue
        url = _dig(attachment, "payload", "url")
        if isinstance(url, str) and url:
            return url
    return None


def _chat(chat_id: Any) -> Chat:
    return Chat(messenger=MessengerKind.MAX, chat_id=str(chat_id))


def _dig(payload: Any, *path: str) -> Any:
    """Достаёт вложенное значение; None, если по дороге что-то не так."""
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
