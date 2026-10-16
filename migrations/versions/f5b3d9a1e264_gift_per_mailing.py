"""Подарок привязан к отправке, с которой пришёл (сессия 13, Д2).

``gifts.mailing_id`` — строка ``mailings``, чья доставка принесла подарок.
Уникальна: одна доставка — один подарок, даже если запись исхода
повторили. Подаркам, начисленным до этой ревизии, отправка подбирается по
человеку и времени: R2 того же человека, доставленное в момент начисления.
До ревизии подарок писался ровно один на доставку (строку отправки заводит
один проход), поэтому двух подарков на одну отправку в данных нет.

Revision ID: f5b3d9a1e264
Revises: e4a2c8f0d153
Create Date: 2026-10-16 20:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f5b3d9a1e264"
down_revision: str | None = "e4a2c8f0d153"
branch_labels: str | None = None
depends_on: str | None = None

#: Отдельной строкой, чтобы тест мог выполнить ровно её и дважды.
BACKFILL_SQL = (
    "UPDATE gifts SET mailing_id = m.id FROM mailings m "
    "WHERE gifts.mailing_id IS NULL AND gifts.source = 'R2' "
    "AND m.user_id = gifts.user_id AND m.step = 'R2' AND m.status = 'sent' "
    "AND m.sent_at = gifts.granted_at"
)


def upgrade() -> None:
    op.add_column(
        "gifts",
        sa.Column(
            "mailing_id",
            sa.BigInteger(),
            sa.ForeignKey("mailings.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_unique_constraint("uq_gifts_mailing", "gifts", ["mailing_id"])
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_constraint("uq_gifts_mailing", "gifts", type_="unique")
    op.drop_column("gifts", "mailing_id")
