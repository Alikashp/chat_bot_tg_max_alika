"""Реализация порта Messenger для Telegram.

Здесь и только здесь ядро встречается с aiogram. Всё остальное приложение
знает лишь протокол из app/ports/messenger.py, поэтому на фазе 7 рядом
появится такой же файл для MAX, и ни один сценарий об этом не узнает.
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message

from app.adapters.telegram import keyboards as tg_keyboards
from app.core.models import Chat, Keyboard, MessageRef, Photo
from app.core.photos import PhotoTooLargeError

#: По сколько байт читаем файл. Больше смысла нет: фото у нас в пределах
#: пяти мегабайт, а меньше — лишние обращения к сети.
_DOWNLOAD_CHUNK = 64 * 1024

#: Таймаут скачивания файла. Явный, как требует §3.4.6.
_DOWNLOAD_TIMEOUT = 30


class TelegramMessenger:
    """Исходящие операции Telegram."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    # --- Отправка ------------------------------------------------------

    async def send_text(
        self,
        chat: Chat,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        message = await self._bot.send_message(
            chat_id=chat.chat_id,
            text=text,
            reply_markup=_markup(keyboard, show_menu),
        )
        return _ref(chat, message)

    async def send_photo(
        self,
        chat: Chat,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        message = await self._bot.send_photo(
            chat_id=chat.chat_id,
            photo=BufferedInputFile(photo.data, filename=photo.filename),
            caption=caption,
            reply_markup=_markup(keyboard, show_menu),
        )
        return _ref(chat, message)

    async def send_photo_by_ref(
        self,
        chat: Chat,
        photo_ref: str,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        """Отправляет уже загруженную картинку по её file_id.

        Байты у Telegram уже есть, и заливать их повторно ради подписи было бы
        лишними секундами ожидания на ровном месте.
        """
        message = await self._bot.send_photo(
            chat_id=chat.chat_id,
            photo=photo_ref,
            caption=caption,
            reply_markup=_markup(keyboard, show_menu),
        )
        return _ref(chat, message)

    # --- Замена уже отправленного --------------------------------------

    async def edit_text(
        self,
        ref: MessageRef,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> None:
        await self._bot.edit_message_text(
            chat_id=ref.chat.chat_id,
            message_id=int(ref.message_id),
            text=text,
            reply_markup=tg_keyboards.inline(keyboard) if keyboard else None,
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

        Сделано удалением и отправкой, а не editMessageMedia. Причина в
        Telegram, а не в нашем удобстве: editMessageMedia работает только по
        сообщениям, которые уже содержат медиа, а «Рисую…» — текст. Попытка
        отредактировать текст в картинку возвращает ошибку всегда, так что
        пробовать её на каждой картинке значило бы честно платить лишним
        вызовом за заведомо известный отказ.

        Результат для пользователя тот же, что требует §2.3: ожидание не
        остаётся в переписке мусором, на его месте оказывается картинка.

        Неудача удаления не отменяет отправку: остаться без результата хуже,
        чем увидеть над ним лишнюю строчку.
        """
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.delete_message(
                chat_id=ref.chat.chat_id, message_id=int(ref.message_id)
            )

        message = await self._bot.send_photo(
            chat_id=ref.chat.chat_id,
            photo=BufferedInputFile(photo.data, filename=photo.filename),
            caption=caption,
            reply_markup=tg_keyboards.inline(keyboard) if keyboard else None,
        )
        return _photo_file_id(message)

    # --- Прочее --------------------------------------------------------

    async def send_typing(self, chat: Chat) -> None:
        await self._bot.send_chat_action(chat_id=chat.chat_id, action="typing")

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None
    ) -> None:
        await self._bot.answer_callback_query(
            callback_query_id=callback_id, text=notification
        )

    async def download_photo(self, photo_ref: str, *, max_bytes: int) -> Photo:
        """Скачивает присланное фото, не давая ему переполнить память.

        Проверок размера две, и обе нужны. Первая — по заявленному размеру из
        getFile: она бесплатная и отсекает большое ещё до скачивания. Вторая —
        по фактически прочитанному: заявленный размер приходит снаружи, а на
        такие числа полагаться нельзя (§3.5).
        """
        file = await self._bot.get_file(photo_ref)
        if file.file_size is not None and file.file_size > max_bytes:
            raise PhotoTooLargeError(photo_ref)
        if file.file_path is None:
            raise RuntimeError("Telegram не вернул путь к файлу")

        url = self._bot.session.api.file_url(self._bot.token, file.file_path)
        chunks: list[bytes] = []
        size = 0
        stream = self._bot.session.stream_content(
            url=url,
            timeout=_DOWNLOAD_TIMEOUT,
            chunk_size=_DOWNLOAD_CHUNK,
            raise_for_status=True,
        )
        async for chunk in stream:
            size += len(chunk)
            if size > max_bytes:
                # Обрываем чтение, а не проверяем в конце: иначе достаточно
                # прислать файл с заниженным размером, чтобы занять память.
                await stream.aclose()
                raise PhotoTooLargeError(photo_ref)
            chunks.append(chunk)

        data = b"".join(chunks)
        return Photo(
            data=data,
            mime_type=_mime_by_path(file.file_path),
            filename=file.file_path.rsplit("/", maxsplit=1)[-1],
        )


# --- Вспомогательное -----------------------------------------------------


def _markup(keyboard: Keyboard | None, show_menu: bool) -> Any:
    """Выбирает единственную клавиатуру, которую разрешает Telegram.

    Inline-кнопки под сообщением важнее: без них экран становится тупиком.
    Постоянное меню от этого не пропадает — reply-клавиатура остаётся на
    экране с предыдущего сообщения.
    """
    if keyboard is not None:
        return tg_keyboards.inline(keyboard)
    if show_menu:
        return tg_keyboards.main_menu()
    return None


def _ref(chat: Chat, message: Message) -> MessageRef:
    return MessageRef(chat=chat, message_id=str(message.message_id))


def _photo_file_id(message: Message) -> str | None:
    """Идентификатор доставленной картинки — для кнопки «Поделиться».

    Берём самый крупный вариант: Telegram отдаёт несколько размеров, а
    пересылать имеет смысл лучший.
    """
    if not message.photo:
        return None
    return message.photo[-1].file_id


#: Расширение файла у Telegram надёжнее заявленного типа, но и оно ничего не
#: гарантирует: формат всё равно проверяется по сигнатуре в core/photos.py.
_MIME_BY_SUFFIX = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _mime_by_path(path: str) -> str:
    suffix = path.rsplit(".", maxsplit=1)[-1].lower() if "." in path else ""
    return _MIME_BY_SUFFIX.get(suffix, "image/jpeg")
