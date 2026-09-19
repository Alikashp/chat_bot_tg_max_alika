"""Отдельный лимит на разбор документов.

Разбор файла не считается ни сообщением, ни картинкой: он стоит заметно
дороже сообщения и не имеет отношения к картинкам, а человеку в профиле
должно быть видно, что именно у него кончилось.

Устроено как у картинок: на бесплатном тарифе дневной нормы нет вовсе,
бесплатное приходит разово и лежит в ``bonus_documents``, который не сгорает.
Дневная норма (``daily_documents`` в реестре тарифов) есть только у платных.

Уже заведённым людям разовая выдача этой миграцией **не** начисляется. Она
выдаётся при регистрации, а раздавать её задним числом всем подряд — это
решение про деньги, а не про схему: захочет заказчик — начислит отдельно и
осознанно.

Revision ID: d5a2c81e7f43
Revises: b2e9c4a1f7d8
Create Date: 2026-09-19 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d5a2c81e7f43"
down_revision: str | None = "b2e9c4a1f7d8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("bonus_documents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_users_bonus_documents", "users", "bonus_documents >= 0"
    )
    op.add_column(
        "usage",
        sa.Column("documents_used", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("usage", "documents_used")
    op.drop_constraint("ck_users_bonus_documents", "users", type_="check")
    op.drop_column("users", "bonus_documents")
