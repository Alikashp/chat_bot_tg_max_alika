"""Чего бот ждёт от пользователя следующим сообщением.

Нажал «🎨 Картинки» — дальше любое сообщение считается описанием картинки, а
не вопросом в чат. Выбрал прикол — дальше ждём фото. Это состояние диалога, и
живёт оно в ядре, а не в механизмах конкретного мессенджера: правило одинаково
для Telegram и MAX, а FSM у aiogram — вещь в себе, которая в MAX не поедет.

Хранится в базе, а не в памяти процесса: иначе выкатка посреди разговора
превращает «Опиши, что нарисовать» в потерянный вопрос — человек пишет
описание, а бот отвечает на него как на реплику в чате.
"""

from __future__ import annotations

#: Ждём описание будущей картинки (§2.3).
AWAIT_IMAGE_PROMPT = "await:image"

#: Ждём фото под выбранный прикол (§2.4). За префиксом — идентификатор пресета.
_AWAIT_PRESET_PREFIX = "await:preset:"


def await_preset(preset_id: str) -> str:
    """Состояние «ждём фото под такой-то прикол»."""
    if not preset_id:
        raise ValueError("нужен идентификатор пресета")
    return f"{_AWAIT_PRESET_PREFIX}{preset_id}"


def parse_await_preset(pending: str | None) -> str | None:
    """Возвращает идентификатор пресета, если ждём фото под него."""
    if pending is None or not pending.startswith(_AWAIT_PRESET_PREFIX):
        return None
    return pending.removeprefix(_AWAIT_PRESET_PREFIX) or None


def is_awaiting_image_prompt(pending: str | None) -> bool:
    """Ждём ли описание картинки."""
    return pending == AWAIT_IMAGE_PROMPT
