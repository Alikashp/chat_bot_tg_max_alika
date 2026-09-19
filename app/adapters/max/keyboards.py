"""Перевод абстрактных клавиатур ядра в клавиатуры MAX.

Главное расхождение с Telegram (docs/research.md §1.6): постоянных клавиатур в
MAX нет вовсе, есть только inline-вложение к конкретному сообщению.

Отсюда и решение: к каждому сообщению цепляется **одна** кнопка «☰ В меню», а
не все пять пунктов. Пять пунктов под каждым ответом заслоняют собой саму
переписку — человек смотрит на присланный файл, а под ним вырастает меню
высотой в экран. Кнопка открывает меню одним нажатием, и оно приходит
отдельным сообщением, где ему и место.

В Telegram этого не происходит: там меню живёт постоянной клавиатурой снизу и
ничего не заслоняет.
"""

from __future__ import annotations

from maxapi.enums.attachment import AttachmentType
from maxapi.types import CallbackButton, LinkButton
from maxapi.types.attachments.attachment import ButtonsPayload
from maxapi.types.attachments.buttons import InlineButtonUnion
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton

from app.core import texts
from app.core.actions import Action
from app.core.models import Button, Keyboard

#: Тип кнопки в MAX. Из восьми возможных нам нужны две: нажатие и ссылка.
InlineButton = InlineButtonUnion


def build(keyboard: Keyboard | None, *, show_menu: bool) -> AttachmentButton | None:
    """Собирает вложение с кнопками; None — если кнопок нет вовсе."""
    rows: list[list[InlineButton]] = []
    if keyboard is not None:
        rows.extend(_row(row) for row in keyboard.rows)
    if show_menu:
        rows.append(
            [CallbackButton(text=texts.BUTTON_SHOW_MENU, payload=Action.MENU_SHOW)]
        )
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
