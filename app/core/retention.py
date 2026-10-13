"""Сколько живёт история переписки (сессия 10, шаг 7).

Память разговора — обещание продукта (spec §4.3): контекст помнится всегда,
и на нём держатся кнопки «Объясни проще» и «Короче». Это единственное
место, где текст человека лежит в базе, и держать его дольше, чем человек
разговором пользуется, незачем: история без активности
``dialog_retention_days`` дней удаляется этим проходом. «Новый диалог»
удаляет её сразу.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.scenarios.deps import Deps


async def forget_idle_dialogs(deps: Deps) -> None:
    """Один проход: удалить историю, молчащую дольше срока."""
    before = deps.now() - timedelta(days=deps.settings.dialog_retention_days)
    forgotten = await deps.storage.forget_idle_dialogs(before)
    if forgotten:
        deps.logger.info("dialogs_forgotten", count=forgotten)
