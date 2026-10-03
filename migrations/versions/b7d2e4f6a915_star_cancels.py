"""Очередь отмен прежних звёздных подписок (фаза 11, часть 3, шаг 0а).

Новая оплата отменяет прежнюю звёздную подписку в Telegram. Если Telegram
отмену не принял, подписка встаёт сюда, и её отмену повторяет каждый проход
биллинга, пока она не пройдёт. Раньше такой сбой оставался ошибкой в логе
для ручного разбора, а ручной отмены у заказчика нет.

Revision ID: b7d2e4f6a915
Revises: a3c9e5f7d218
Create Date: 2026-10-03 18:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e4f6a915"
down_revision: str | None = "a3c9e5f7d218"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "star_cancels",
        sa.Column("charge_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("star_cancels")
