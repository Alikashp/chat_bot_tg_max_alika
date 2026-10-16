"""Подарки со сроком (сессия 12, шаг 4): презентация из R2 на 3 суток.

Строка на подарок: ``granted_at`` — начислен, ``used_at`` — использован,
``expired_at`` — сгорел. Заполнять нечего: подарков до этой ревизии не было.

Revision ID: e4a2c8f0d153
Revises: d1f7b3c9e042
Create Date: 2026-10-16 18:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e4a2c8f0d153"
down_revision: str | None = "d1f7b3c9e042"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "gifts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gifts_user_id", "gifts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_gifts_user_id", table_name="gifts")
    op.drop_table("gifts")
