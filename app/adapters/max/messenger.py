"""Реализация порта Messenger для MAX.

Второй файл в проекте, где ядро встречается с чужой библиотекой, — и
единственное, что понадобилось написать заново, чтобы весь продукт заработал
во втором мессенджере. Сценарии, лимиты, тексты и маршрутизация те же самые.

Из maxapi берётся только транспортный класс Bot. Его диспетчер, роутеры и
webhook-интеграции не используются: роутинг у нас один и живёт в core
(docs/research.md §2).
"""

from __future__ import annotations

import httpx
from maxapi import Bot
from maxapi.enums.sender_action import SenderAction
from maxapi.enums.upload_type import UploadType
from maxapi.types import InputMediaBuffer
from maxapi.types.attachments.image import Image
from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload

from app.adapters.max import keyboards as max_keyboards
from app.core.models import Chat, Keyboard, MessageRef, Photo
from app.core.photos import PhotoTooLargeError

#: По сколько байт читаем присланное фото.
_DOWNLOAD_CHUNK = 64 * 1024


class MaxMessengerError(RuntimeError):
    """MAX принял запрос, но не вернул того, за чем обращались."""


class MaxMessenger:
    """Исходящие операции MAX."""

    def __init__(self, bot: Bot, http: httpx.AsyncClient) -> None:
        self._bot = bot
        # Отдельный HTTP-клиент нужен ровно для скачивания присланных фото:
        # у maxapi есть download_bytes, но он читает файл целиком, без
        # потолка размера, а §3.5 требует отказать до загрузки в память.
        self._http = http

    # --- Отправка ------------------------------------------------------

    async def send_text(
        self,
        chat: Chat,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        return await self._send(chat, text=text, keyboard=keyboard, show_menu=show_menu)

    async def send_photo(
        self,
        chat: Chat,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        uploaded = await self._upload(photo)
        return await self._send(
            chat,
            text=caption,
            keyboard=keyboard,
            show_menu=show_menu,
            image=uploaded,
        )

    async def send_photo_by_ref(
        self,
        chat: Chat,
        photo_ref: str,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        """Отправляет уже загруженную картинку по её токену.

        Токен выдаётся при загрузке и годится для повторной отправки — заливать
        те же байты второй раз незачем.
        """
        return await self._send(
            chat,
            text=caption,
            keyboard=keyboard,
            show_menu=show_menu,
            image=_by_token(photo_ref),
        )

    # --- Замена уже отправленного --------------------------------------

    async def edit_text(
        self,
        ref: MessageRef,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> None:
        await self._bot.edit_message(
            message_id=ref.message_id,
            text=text,
            attachments=_attachments(keyboard, show_menu=True),
        )

    async def edit_to_photo(
        self,
        ref: MessageRef,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
    ) -> str | None:
        """Заменяет «Рисую…» готовой картинкой.

        В MAX это настоящее редактирование, а не «удалить и отправить заново»,
        как пришлось сделать в Telegram: edit_message здесь принимает
        вложения и умеет превратить текст в картинку (docs/research.md §1.6).

        Возвращает токен загруженной картинки — по нему работает «Поделиться».
        Ответ на редактирование самих вложений не содержит, поэтому токен
        берём из загрузки, а не из ответа.
        """
        uploaded = await self._upload(photo)
        await self._bot.edit_message(
            message_id=ref.message_id,
            text=caption,
            attachments=[uploaded, *_attachments(keyboard, show_menu=True)],
        )
        return uploaded.payload.token

    # --- Прочее --------------------------------------------------------

    async def send_typing(self, chat: Chat) -> None:
        await self._bot.send_action(
            chat_id=int(chat.chat_id), action=SenderAction.TYPING_ON
        )

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None
    ) -> None:
        await self._bot.send_callback(
            callback_id=callback_id, notification=notification
        )

    async def download_photo(self, photo_ref: str, *, max_bytes: int) -> Photo:
        """Скачивает присланное фото по его адресу.

        В MAX идентификатор присланной картинки — это её URL из вложения
        (docs/research.md §1.7). Читаем потоком и обрываем чтение, как только
        размер вышел за предел: файл не должен попасть в память целиком, чтобы
        потом быть отвергнутым (§3.5).
        """
        chunks: list[bytes] = []
        size = 0
        async with self._http.stream("GET", photo_ref) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise PhotoTooLargeError(photo_ref)

            async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise PhotoTooLargeError(photo_ref)
                chunks.append(chunk)

        return Photo(
            data=b"".join(chunks),
            mime_type=_mime_of(response.headers.get("content-type")),
            filename="photo.jpg",
        )

    # --- Внутреннее ----------------------------------------------------

    async def _send(
        self,
        chat: Chat,
        *,
        text: str | None,
        keyboard: Keyboard | None,
        show_menu: bool,
        image: AttachmentUpload | Image | None = None,
    ) -> MessageRef:
        attachments = _attachments(keyboard, show_menu=show_menu)
        if image is not None:
            attachments = [image, *attachments]

        sent = await self._bot.send_message(
            chat_id=int(chat.chat_id),
            text=text,
            attachments=attachments or None,
        )
        if sent is None or sent.message.body is None:
            raise MaxMessengerError("MAX не вернул отправленное сообщение")
        return MessageRef(chat=chat, message_id=sent.message.body.mid)

    async def _upload(self, photo: Photo) -> AttachmentUpload:
        """Загружает байты и возвращает готовое вложение.

        Загрузка в MAX двухшаговая: сначала адрес, потом сами байты
        (docs/research.md §1.7). maxapi прячет оба шага в upload_media.
        """
        return await self._bot.upload_media(
            InputMediaBuffer(
                buffer=photo.data,
                filename=photo.filename,
                type=UploadType.IMAGE,
            )
        )


def _attachments(keyboard: Keyboard | None, *, show_menu: bool) -> list:  # type: ignore[type-arg]
    """Кнопки под сообщением. Пустой список — если кнопок нет."""
    buttons = max_keyboards.build(keyboard, show_menu=show_menu)
    return [buttons] if buttons is not None else []


def _by_token(token: str) -> AttachmentUpload:
    """Вложение из уже загруженной картинки."""
    return AttachmentUpload(
        type=UploadType.IMAGE, payload=AttachmentPayload(token=token)
    )


def _declared_size(header: str | None) -> int:
    """Заявленный размер файла. Ноль — если его не сообщили.

    Проверка по нему бесплатна и отсекает большое до чтения. Верить ей нельзя:
    заявленный размер приходит снаружи, поэтому дальше считаем и фактический.
    """
    return int(header) if header is not None and header.isdigit() else 0


def _mime_of(content_type: str | None) -> str:
    """Заявленный тип. Настоящий всё равно проверяется по сигнатуре файла."""
    if content_type is None:
        return "image/jpeg"
    return content_type.split(";")[0].strip() or "image/jpeg"
