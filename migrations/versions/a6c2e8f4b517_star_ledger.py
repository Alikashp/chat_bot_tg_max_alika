"""Позиция сверки журнала звёздных транзакций (сессия 10, шаг 1).

Сообщение об оплате звёздами Telegram присылает один раз. Если обработка
упала или сервис перезапустился, оплату доводит сверка по журналу
транзакций бота; здесь — до какой позиции журнал уже сверен. Начинается с
нуля: первый проход читает журнал целиком и доводит всё, что потерялось
раньше.

Revision ID: a6c2e8f4b517
Revises: f2b8d4a6c391
Create Date: 2026-10-12 18:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a6c2e8f4b517"
down_revision: str | None = "f2b8d4a6c391"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "star_ledger",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("offset", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("star_ledger")
