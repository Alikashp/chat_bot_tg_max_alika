"""Заказ автосписания, исход которого ещё не известен (П8).

Раньше каждый проход списаний заводил новый заказ, а значит и новый ключ
идемпотентности. Если ЮKassa деньги списала, а ответ до нас не дошёл, повтор
с новым ключом списывал второй раз. Теперь заказ периода запоминается в
подписке и повторяется он же, пока исход не станет известен.

NULL у всех существующих подписок: незавершённых списаний на момент выкладки
нет — прежний код их не помнил.

Revision ID: e1a7c3d9b204
Revises: d4f1e8b2c6a7
Create Date: 2026-10-02 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e1a7c3d9b204"
down_revision: str | None = "d4f1e8b2c6a7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("charge_order_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "charge_order_id")
