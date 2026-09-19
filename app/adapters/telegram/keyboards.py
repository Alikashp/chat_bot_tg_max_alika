"""Перевод абстрактных клавиатур ядра в клавиатуры Telegram.

Ядро отдаёт Keyboard и флаг show_menu и не знает, во что они превратятся.
Здесь это знание и живёт: постоянное меню рисуется reply-клавиатурой, всё
остальное — inline-кнопками под сообщением.

Одно ограничение Telegram определяет всю конструкцию: у сообщения может быть
ровно одна клавиатура — либо reply, либо inline. Совместить нельзя. Но
reply-клавиатура остаётся на экране после отправки и живёт до следующей
замены, поэтому меню никуда не девается, пока под сообщением висят inline-
кнопки. Так и выполняется §2.1: меню доступно с любого экрана.
"""

from __future__ import annotations

from collections.abc import Mapping

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.adapters.telegram import emoji as tg_emoji
from app.core.models import Button, Keyboard
from app.core.scenarios import keyboards as core_keyboards


def inline(
    keyboard: Keyboard, premium_emoji: Mapping[str, str] | None = None
) -> InlineKeyboardMarkup:
    """Кнопки под конкретным сообщением.

    ``premium_emoji`` — ведущие эмодзи, которым Telegram нарисует премиальную
    иконку вместо символа. Пусто — кнопки те же, что и раньше.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(button, premium_emoji or {}) for button in row]
            for row in keyboard.rows
        ]
    )


def _button(button: Button, premium_emoji: Mapping[str, str]) -> InlineKeyboardButton:
    """Одна кнопка: ссылка или действие, с иконкой или без."""
    text, emoji_id = tg_emoji.icon(button.text, premium_emoji)
    if button.action is None:
        return InlineKeyboardButton(
            text=text, url=button.url, icon_custom_emoji_id=emoji_id
        )
    return InlineKeyboardButton(
        text=text, callback_data=button.action, icon_custom_emoji_id=emoji_id
    )


def main_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню из четырёх кнопок (§2.1).

    Нажатие возвращается обычным текстом — самой подписью кнопки. Обратно в
    действие его переводит core/scenarios/keyboards.py::action_for_label,
    поэтому подписи здесь и там не могут разойтись: источник один.

    Премиальных иконок здесь поэтому и нет. Иконка ставится вместо эмодзи в
    подписи, а подпись — это единственное, по чему нажатие опознаётся: убрав
    из неё эмодзи, мы получили бы обратно текст, которого нет в таблице, и
    меню перестало бы работать целиком. Цена иконки — четыре мёртвые кнопки.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=button.text) for button in row]
            for row in core_keyboards.main_menu().rows
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
