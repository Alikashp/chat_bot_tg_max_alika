"""Напоминание о списании — только где оно положено (фаза 11, часть 3, 0г).

Решение заказчика: перед обычными продлениями напоминаний нет, и списание
от них не зависит. Напоминание остаётся только перед первым списанием
полной цены после пробного периода — его подписка отмечает этой колонкой.
У всех существующих подписок — false: они обычные.

Revision ID: c8e3f5a7b026
Revises: b7d2e4f6a915
Create Date: 2026-10-03 19:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3f5a7b026"
down_revision: str | None = "b7d2e4f6a915"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "remind_before_charge",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "remind_before_charge")
