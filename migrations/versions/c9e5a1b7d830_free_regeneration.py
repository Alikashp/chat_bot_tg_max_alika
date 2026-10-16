"""Бесплатная перегенерация первой бесплатной презентации (сессия 12).

``users.first_deck_at`` — когда человеку доставили первую презентацию,
``users.free_regen_at`` — когда он потратил единственную бесплатную
перегенерацию. Тем, кто презентации уже получал, первая колода
проставляется по учёту обращений (``generations``, удачная сборка): их
следующая колода — не первая, и бесплатной перегенерации у неё нет.
Повторный прогон ничего не меняет: заполняются только пустые отметки.

Revision ID: c9e5a1b7d830
Revises: b8d4f0a6c729
Create Date: 2026-10-16 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5a1b7d830"
down_revision: str | None = "b8d4f0a6c729"
branch_labels: str | None = None
depends_on: str | None = None

#: Отдельной строкой, чтобы тест мог выполнить ровно её и дважды.
BACKFILL_SQL = (
    "UPDATE users SET first_deck_at = first.at FROM ("
    "SELECT user_id, min(created_at) AS at FROM generations "
    "WHERE kind = 'presentation' AND status = 'success' GROUP BY user_id"
    ") AS first WHERE users.id = first.user_id AND users.first_deck_at IS NULL"
)


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("first_deck_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users", sa.Column("free_regen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_column("users", "free_regen_at")
    op.drop_column("users", "first_deck_at")
