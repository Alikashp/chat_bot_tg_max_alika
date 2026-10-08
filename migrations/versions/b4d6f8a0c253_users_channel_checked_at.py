"""Обязательная подписка на канал (сессия 8, шаг 3).

``users.channel_checked_at`` — когда Telegram в последний раз подтвердил
подписку на канал; в пределах настроенного срока заново не спрашиваем.

``users.channel_bonus_at`` остаётся как есть: бонус за канал убран, но
отметки о прошлых выдачах — история, а начисленные картинки лежат в
``bonus_images`` и никуда не деваются.

Revision ID: b4d6f8a0c253
Revises: a3c5e7f9b142
Create Date: 2026-10-08 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b4d6f8a0c253"
down_revision: str | None = "a3c5e7f9b142"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("channel_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "channel_checked_at")
