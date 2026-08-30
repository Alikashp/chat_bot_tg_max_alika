"""Перевод абстрактных клавиатур ядра в клавиатуры MAX.

Главное расхождение с Telegram (docs/research.md §1.6): постоянных клавиатур в
MAX нет вовсе, есть только inline-вложение к конкретному сообщению. Поэтому
четвёрка главного меню прикрепляется к каждому сообщению заново.

Из этого следует и приятное отличие. В Telegram у сообщения может быть только
одна клавиатура, и кнопки под сообщением вытесняют меню. Здесь обе живут в
одном вложении: сначала кнопки экрана, под ними меню. Пользователю в MAX
доступно и то и другое одновременно.
"""

from __future__ import annotations

from maxapi.enums.attachment import AttachmentType
from maxapi.types import CallbackButton, LinkButton
from maxapi.types.attachments.attachment import ButtonsPayload
from maxapi.types.attachments.buttons import InlineButtonUnion
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton

from app.core.models import Button, Keyboard
from app.core.scenarios import keyboards as core_keyboards

#: Тип кнопки в MAX. Из восьми возможных нам нужны две: нажатие и ссылка.
InlineButton = InlineButtonUnion


def build(keyboard: Keyboard | None, *, show_menu: bool) -> AttachmentButton | None:
    """Собирает вложение с кнопками; None — если кнопок нет вовсе."""
    rows: list[list[InlineButton]] = []
    if keyboard is not None:
        rows.extend(_row(row) for row in keyboard.rows)
    if show_menu:
        rows.extend(_row(row) for row in core_keyboards.main_menu().rows)
    if not rows:
        return None
    return AttachmentButton(
        type=AttachmentType.INLINE_KEYBOARD,
        payload=ButtonsPayload(buttons=rows),
        bot=None,
    )


def _row(row: tuple[Button, ...]) -> list[InlineButton]:
    return [_button(button) for button in row]


def _button(button: Button) -> InlineButton:
    if button.action is None:
        return LinkButton(text=button.text, url=button.url)
    # payload — прямой аналог callback_data: значение вернётся боту при
    # нажатии, и разбирает его тот же общий маршрутизатор.
    return CallbackButton(text=button.text, payload=button.action)
