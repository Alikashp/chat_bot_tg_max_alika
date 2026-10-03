"""Пробный период тарифа «Лайт» (фаза 11, часть 3, шаг 1).

``payments.trial`` отмечает заказ на 1 ₽ за три дня с сохранением карты.
``users.trial_order_id`` — заказ, по которому пробный период выдан; пусто —
не выдавали. Отметка ставится той же транзакцией, что и выдача, и только
если она пуста: так пробный период достаётся человеку один раз.

У существующих заказов — false, у людей — NULL: пробного периода ещё ни у
кого не было, а кто уже платил, тому он и так не положен — это проверяется
по заказам.

Revision ID: d9f4a6b8c137
Revises: c8e3f5a7b026
Create Date: 2026-10-03 20:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d9f4a6b8c137"
down_revision: str | None = "c8e3f5a7b026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("trial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("trial_order_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "trial_order_id")
    op.drop_column("payments", "trial")
