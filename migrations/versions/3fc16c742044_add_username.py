"""add username

Имя пользователя в мессенджере — для поддержки: по обращению надо уметь найти
человека. NOT NULL со значением по умолчанию, потому что «имени нет» — это
тоже ответ, а пустая ячейка одинаково выглядит и как «нет», и как «не
записали». Существующие строки получают NONE: до этой миграции имя не
собиралось вовсе.

Revision ID: 3fc16c742044
Revises: 6ed956ac7f7c
Create Date: 2026-09-03 06:40:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "3fc16c742044"
down_revision: str | None = "6ed956ac7f7c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "username",
            sa.String(length=64),
            nullable=False,
            server_default="NONE",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "username")
