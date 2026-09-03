"""drop unused referred_by

Колонка заводилась на фазе 1 и не записывалась ни разу: кто кого пригласил,
живёт в таблице ``referrals``, где у связи есть и уникальность, и время для
суточного потолка наград. Поле у пользователя было вторым источником правды,
который всегда оставался пустым, — и однажды кто-нибудь на него положился бы.

Revision ID: 6ed956ac7f7c
Revises: 6a0476c23f1b
Create Date: 2026-09-03 05:12:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "6ed956ac7f7c"
down_revision: str | None = "6a0476c23f1b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("users", "referred_by")


def downgrade() -> None:
    # Возвращается пустой: значений в ней и не было.
    op.add_column(
        "users",
        sa.Column("referred_by", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "users_referred_by_fkey", "users", "users", ["referred_by"], ["id"]
    )
