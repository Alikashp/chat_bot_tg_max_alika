"""Темы презентаций больше не хранятся в базе (сессия 7, В6).

До экрана параметров тема презентации лежала в двух местах базы: в
ожидании выбора оформления (``users.pending`` вида ``await:pres:theme:<тема>``)
и в контексте «Повторить» (``users.retry_context``, поле ``prompt``). Теперь
всё это живёт в черновике в памяти процесса, а в базе — только жетон.

Миграция стирает то, что успела записать прежняя версия. Повторный прогон
ничего не находит: запросы условные, и найти что-то второй раз им нечего.
Ожидание оформления по докладу (``await:pres:from:<жетон>``) темы не
содержит, но и работать больше не может — его тоже снимаем.

Обратной миграции у данных нет: стёртое не вернуть, да и незачем.

Revision ID: e0a5b7c9d248
Revises: d9f4a6b8c137
Create Date: 2026-10-04 12:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op

revision: str = "e0a5b7c9d248"
down_revision: str | None = "d9f4a6b8c137"
branch_labels: str | None = None
depends_on: str | None = None

#: Отдельными строками, чтобы тест мог выполнить ровно их и дважды.
CLEAR_PENDING_SQL = (
    "UPDATE users SET pending = NULL "
    "WHERE pending LIKE 'await:pres:theme:%' OR pending LIKE 'await:pres:from:%'"
)
CLEAR_RETRY_SQL = (
    "UPDATE users SET retry_context = NULL "
    'WHERE retry_context LIKE \'%"kind":"presentation"%\''
)


def upgrade() -> None:
    op.execute(CLEAR_PENDING_SQL)
    op.execute(CLEAR_RETRY_SQL)


def downgrade() -> None:
    """Стёртые темы не восстанавливаются: их нет нигде, и это и было целью."""
