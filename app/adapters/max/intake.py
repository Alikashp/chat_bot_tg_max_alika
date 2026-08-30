"""Ключ дедупликации для обновлений MAX.

Отдельный файл по той же причине, что и у Telegram: ключ вычисляется до
очереди, чтобы повтор не занимал в ней место.

Но задача здесь принципиально сложнее. У Telegram есть сквозной ``update_id``,
растущий и уникальный. У MAX такого поля нет вовсе: в базовой модели
обновления только ``update_type`` и ``timestamp`` (docs/research.md §1.4).
Поэтому ключ приходится собирать из полей самого события — по одному правилу
на каждый тип.
"""

from __future__ import annotations

from typing import Any

#: Типы обновлений, которые бот обрабатывает. Остальные приходить не должны:
#: подписываемся только на эти три.
MESSAGE_CREATED = "message_created"
MESSAGE_CALLBACK = "message_callback"
BOT_STARTED = "bot_started"


def dedup_key(raw_update: dict[str, Any]) -> str | None:
    """Ключ дедупликации обновления MAX.

    Возвращает None, если событие не похоже на настоящее или относится к типу,
    на который мы не подписывались, — такое обновление разбирать нечего.
    """
    update_type = raw_update.get("update_type")
    if not isinstance(update_type, str):
        return None

    match update_type:
        case "message_created":
            # mid — идентификатор самого сообщения, уникальный в пределах MAX.
            mid = _dig(raw_update, "message", "body", "mid")
            return f"max:msg:{mid}" if mid else None
        case "message_callback":
            # callback_id уникален для каждого нажатия.
            callback_id = _dig(raw_update, "callback", "callback_id")
            return f"max:cb:{callback_id}" if callback_id else None
        case "bot_started":
            # Своего идентификатора у события нет. Составляем из чата и
            # отметки времени: один и тот же человек не запускает бота дважды
            # в одну и ту же секунду, а повторная доставка приходит с той же
            # отметкой — именно её нам и надо отсечь.
            chat_id = raw_update.get("chat_id")
            timestamp = raw_update.get("timestamp")
            if chat_id is None or timestamp is None:
                return None
            return f"max:start:{chat_id}:{timestamp}"
        case _:
            return None


def _dig(payload: dict[str, Any], *path: str) -> str | None:
    """Достаёт вложенную строку; None, если по дороге что-то не так."""
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) and current else None
