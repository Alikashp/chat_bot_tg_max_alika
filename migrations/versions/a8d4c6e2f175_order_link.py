"""Выданная ссылка на оплату и почта её чека (сессия 14, шаг 1).

``payments.confirmation_url`` — ссылка, которую человек получил;
``payments.receipt_email`` — почта, на которую уйдёт чек этой ссылки.
Повторное нажатие кнопки покупки отдаёт ту же ссылку вместо второй рабочей.
Прежним заказам заполнять нечего: ссылки до этой ревизии не хранились, и
для них действует прежнее поведение — новая ссылка.

Revision ID: a8d4c6e2f175
Revises: f5b3d9a1e264
Create Date: 2026-10-23 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4c6e2f175"
down_revision: str | None = "f5b3d9a1e264"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("confirmation_url", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("receipt_email", sa.String(254), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "receipt_email")
    op.drop_column("payments", "confirmation_url")
