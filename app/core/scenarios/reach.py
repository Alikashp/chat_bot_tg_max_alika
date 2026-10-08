"""Отметка «человек остановил бота» по отказу доставки (сессия 8, шаг 2).

Мессенджер говорит о блокировке двумя путями: событием (его разбирает
маршрутизатор) и отказом доставить сообщение. Второе случается где угодно —
в ответе на вопрос, в напоминании о списании, в уведомлении об оплате, — и
ловить его в каждом сценарии значило бы однажды где-то забыть. Поэтому
отказ ловится один раз, здесь: обёртка над портом мессенджера отмечает
человека и пробрасывает отказ дальше — делать вид, что сообщение дошло,
нельзя.

Адресат — по самой переписке: в Telegram её номер и есть номер человека, у
разговора, который начинаем мы, адресат — сам человек (``is_person``), а в
MAX хозяина переписки называет поле ``person``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from app.core.models import Chat, Document, Keyboard, MessageRef, Photo
from app.ports.messenger import Messenger, RecipientGoneError
from app.ports.observability import Logger
from app.ports.storage import Storage


class WatchedMessenger:
    """Порт Messenger, который замечает «человек остановил бота»."""

    def __init__(
        self,
        inner: Messenger,
        *,
        storage: Storage,
        logger: Logger,
        now: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._storage = storage
        self._logger = logger
        self._now = now

    async def send_text(
        self,
        chat: Chat,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        try:
            return await self._inner.send_text(
                chat, text, keyboard=keyboard, show_menu=show_menu
            )
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def send_photo(
        self,
        chat: Chat,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        try:
            return await self._inner.send_photo(
                chat, photo, caption=caption, keyboard=keyboard, show_menu=show_menu
            )
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def send_album(self, chat: Chat, photos: Sequence[Photo]) -> None:
        try:
            await self._inner.send_album(chat, photos)
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def edit_text(
        self,
        ref: MessageRef,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> None:
        try:
            await self._inner.edit_text(ref, text, keyboard=keyboard)
        except RecipientGoneError:
            await self._mark(ref.chat)
            raise

    async def edit_to_photo(
        self,
        ref: MessageRef,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
    ) -> str | None:
        try:
            return await self._inner.edit_to_photo(
                ref, photo, caption=caption, keyboard=keyboard
            )
        except RecipientGoneError:
            await self._mark(ref.chat)
            raise

    async def send_photo_by_ref(
        self,
        chat: Chat,
        photo_ref: str,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        try:
            return await self._inner.send_photo_by_ref(
                chat, photo_ref, caption=caption, keyboard=keyboard, show_menu=show_menu
            )
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def send_document(self, chat: Chat, document: Document) -> None:
        try:
            await self._inner.send_document(chat, document)
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def download_document(self, document_ref: str, *, max_bytes: int) -> Document:
        return await self._inner.download_document(document_ref, max_bytes=max_bytes)

    async def send_typing(self, chat: Chat) -> None:
        try:
            await self._inner.send_typing(chat)
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def download_photo(self, photo_ref: str, *, max_bytes: int) -> Photo:
        return await self._inner.download_photo(photo_ref, max_bytes=max_bytes)

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None
    ) -> None:
        await self._inner.answer_callback(callback_id, notification=notification)

    async def refresh_menu(self, chat: Chat) -> None:
        try:
            await self._inner.refresh_menu(chat)
        except RecipientGoneError:
            await self._mark(chat)
            raise

    async def _mark(self, chat: Chat) -> None:
        """Отмечает хозяина переписки. Сама отметка не роняет ничего."""
        external = chat.chat_id if chat.is_person else chat.person
        try:
            user = (
                await self._storage.get_user(chat.messenger, external)
                if external is not None
                else None
            )
            if user is None:
                self._logger.warning("user_stopped_bot_unknown")
                return
            await self._storage.mark_stopped(user.id, self._now())
            self._logger.info("user_stopped_bot", user_id=int(user.id), source="send")
        except Exception as error:
            self._logger.warning("user_stopped_mark_failed", error=repr(error))
