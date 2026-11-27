"""Отметка «о конце этого пакета уже сказали» (сессия 16, шаг 3).

За сутки до конца пакета человеку уходит предложение купить снова — один
раз на срок. Отметка хранит срок, о котором сказали: новый срок после
покупки — новое предупреждение. NULL — ещё не говорили.

Revision ID: c9d2e4b6a813
Revises: b3e7f1a9c402
Create Date: 2026-11-27 13:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2e4b6a813"
down_revision: str | None = "b3e7f1a9c402"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("package_notified_for", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "package_notified_for")
