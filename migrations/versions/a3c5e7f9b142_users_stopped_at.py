"""Отметка «человек остановил бота» (сессия 8, шаг 2).

``users.stopped_at`` — когда человек заблокировал, остановил или удалил бота:
по событию мессенджера или по отказу доставить ему сообщение. NULL — бот до
человека достаёт. У всех нынешних — NULL: прошлых событий бот не получал.

Revision ID: a3c5e7f9b142
Revises: e0a5b7c9d248
Create Date: 2026-10-08 10:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b142"
down_revision: str | None = "e0a5b7c9d248"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "stopped_at")
