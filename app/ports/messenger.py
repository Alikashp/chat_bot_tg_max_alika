"""Порт мессенджера.

Ядро знает только этот протокол. Слова «reply-клавиатура», «inline», chat_id
конкретного мессенджера, aiogram и maxapi здесь не встречаются: как нарисовать
клавиатуру и каким вызовом отправить сообщение — знает адаптер.
"""

from __future__ import annotations

from typing import Protocol

from app.core.models import Chat, Keyboard, MessageRef, Photo


class Messenger(Protocol):
    """Исходящие операции, нужные продуктовым сценариям."""

    async def send_text(
        self,
        chat: Chat,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        """Отправляет текст.

        ``show_menu`` требует показать постоянное меню из 4 кнопок (§2.1).
        В Telegram это reply-клавиатура, в MAX — inline-клавиатура, которую
        приходится прикреплять к каждому сообщению; для ядра разницы нет.
        """
        ...

    async def send_photo(
        self,
        chat: Chat,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
        show_menu: bool = True,
    ) -> MessageRef:
        """Отправляет картинку."""
        ...

    async def edit_text(
        self,
        ref: MessageRef,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> None:
        """Заменяет текст ранее отправленного сообщения."""
        ...

    async def edit_to_photo(
        self,
        ref: MessageRef,
        photo: Photo,
        *,
        caption: str | None = None,
        keyboard: Keyboard | None = None,
    ) -> None:
        """Заменяет сообщение картинкой.

        Нужно для сценария §2.3: «Рисую… ~15 сек» превращается в картинку
        редактированием, а не новым сообщением. Проверено, что это умеют оба
        мессенджера (docs/handoff/фаза-1.md).
        """
        ...

    async def send_typing(self, chat: Chat) -> None:
        """Показывает статус «печатает…»."""
        ...

    async def download_photo(self, file_id: str, *, max_bytes: int) -> Photo:
        """Скачивает присланное пользователем фото.

        Реализация обязана отказать, если файл больше ``max_bytes``, — до
        того, как байты попадут в память целиком.
        """
        ...

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None
    ) -> None:
        """Подтверждает нажатие кнопки, чтобы у пользователя погас индикатор."""
        ...
