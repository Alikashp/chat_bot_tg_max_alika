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

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.core.models import Keyboard
from app.core.scenarios import keyboards as core_keyboards


def inline(keyboard: Keyboard) -> InlineKeyboardMarkup:
    """Кнопки под конкретным сообщением."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button.text, url=button.url)
                if button.action is None
                else InlineKeyboardButton(text=button.text, callback_data=button.action)
                for button in row
            ]
            for row in keyboard.rows
        ]
    )


def main_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню из четырёх кнопок (§2.1).

    Нажатие возвращается обычным текстом — самой подписью кнопки. Обратно в
    действие его переводит core/scenarios/keyboards.py::action_for_label,
    поэтому подписи здесь и там не могут разойтись: источник один.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=button.text) for button in row]
            for row in core_keyboards.main_menu().rows
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
