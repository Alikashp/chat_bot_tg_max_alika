"""add support number and docs version

Revision ID: ba58a236f719
Revises: 70ef677f49ff
Create Date: 2026-08-31 09:20:44.580726+00:00
"""

from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

revision: str = "ba58a236f719"
down_revision: str | None = "70ef677f49ff"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Номер для поддержки и версия документов, принятых при оплате.

    Номер добавляется в три шага, а не одним: колонка сперва пустая, потом
    заполняется существующим людям, и только после этого становится
    обязательной. Иначе миграция не пройдёт на непустой базе — а она уже
    непустая.
    """
    op.add_column("users", sa.Column("support_number", sa.Integer(), nullable=True))

    connection = op.get_bind()
    taken: set[int] = set()
    for (user_id,) in connection.execute(sa.text("SELECT id FROM users")):
        number = _free_number(taken)
        connection.execute(
            sa.text("UPDATE users SET support_number = :number WHERE id = :id"),
            {"number": number, "id": user_id},
        )

    op.alter_column("users", "support_number", nullable=False)
    op.create_unique_constraint("uq_users_support_number", "users", ["support_number"])

    # Версия документов, с которыми человек согласился, оформляя заказ.
    # Хранится у платежа, а не у пользователя: документы меняются, и важно,
    # какая редакция действовала в момент конкретной оплаты.
    op.add_column("payments", sa.Column("docs_version", sa.String(32), nullable=True))


def _free_number(taken: set[int]) -> int:
    """Свободный шестизначный номер. Дубликаты в пределах наката исключаем."""
    while True:
        number = secrets.randbelow(999_999 - 100_000 + 1) + 100_000
        if number not in taken:
            taken.add(number)
            return number


def downgrade() -> None:
    op.drop_column("payments", "docs_version")
    op.drop_constraint("uq_users_support_number", "users", type_="unique")
    op.drop_column("users", "support_number")
