"""add email

Почта покупателя — единственный способ доставить фискальный чек (54-ФЗ).
Спрашивается перед первой оплатой картой и переиспользуется автосписаниями,
где спросить уже не у кого.

NULL допустим и означает ровно «картой не платил»: у звёзд чек выставляет
мессенджер, и адрес там ни к чему. Значения по умолчанию нет намеренно —
пустая строка выглядела бы как адрес, которого нет, и однажды уехала бы в
чек.

Revision ID: c4f1a8b7d305
Revises: 9c1b4a7de2f0
Create Date: 2026-09-04 09:20:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1a8b7d305"
down_revision: str | None = "9c1b4a7de2f0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(length=254), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "email")
