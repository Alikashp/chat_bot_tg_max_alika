"""Разбор входящих обновлений Telegram на транспортном уровне.

Здесь только то, что нужно решить до постановки задачи в очередь: чем это
обновление отличается от других. Разбор содержимого — забота сценариев.
"""

from __future__ import annotations

from typing import Any


def dedup_key(raw_update: dict[str, Any]) -> str | None:
    """Ключ дедупликации обновления Telegram.

    У Telegram есть сквозной ``update_id``, растущий и уникальный в пределах
    бота, — этого достаточно. В MAX такого поля нет, и ключ придётся собирать
    из полей события (docs/research.md §1.4); поэтому ключ и вычисляется в
    адаптере, а не в общем коде.

    Возвращает None, если ``update_id`` отсутствует или не число: такое
    обновление Telegram не присылает, а значит это чужой запрос.
    """
    update_id = raw_update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return None
    return f"tg:{update_id}"


def is_pre_checkout(raw_update: dict[str, Any]) -> bool:
    """Спрашивает ли Telegram разрешения на оплату.

    На такой вопрос Telegram ждёт ответа десять секунд, а потом отменяет
    платёж. Поэтому его нельзя ставить в общую очередь за чужими
    генерациями: когда все обработчики заняты минутной картинкой, человек
    с открытой формой оплаты получил бы отказ банка вместо тарифа.
    """
    return isinstance(raw_update.get("pre_checkout_query"), dict)
