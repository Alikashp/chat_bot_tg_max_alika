"""Нормы пакета у человека (сессия 16, шаг 2).

Покупка пакета поверх действующего складывает остатки, и таких чисел в
реестре тарифов нет — они хранятся у человека: сообщения в день, картинки,
доклады и презентации на срок пакета. NULL — нормы берутся из реестра.
Четыре колонки заполняются и очищаются вместе; прежним строкам заполнять
нечего.

Revision ID: b3e7f1a9c402
Revises: a8d4c6e2f175
Create Date: 2026-11-27 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7f1a9c402"
down_revision: str | None = "a8d4c6e2f175"
branch_labels: str | None = None
depends_on: str | None = None

COLUMNS = ("pack_messages", "pack_images", "pack_documents", "pack_presentations")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("users", sa.Column(name, sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_users_pack_all_or_none",
        "users",
        "(pack_messages IS NULL) = (pack_images IS NULL) "
        "AND (pack_images IS NULL) = (pack_documents IS NULL) "
        "AND (pack_documents IS NULL) = (pack_presentations IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_pack_all_or_none", "users", type_="check")
    for name in reversed(COLUMNS):
        op.drop_column("users", name)
