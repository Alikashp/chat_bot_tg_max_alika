"""Активность людей и отправки рассылки реактивации (сессия 12, шаг 2).

``users.last_active_at`` — последнее входящее от человека,
``users.last_marketing_at`` — последнее маркетинговое сообщение, таблица
``mailings`` — отправки шагов R1–R3 без текста сообщений.

Активность заполняется по фактическим следам: регистрация, учёт обращений
к провайдерам (``generations``), расход норм (``usage``: день с
потраченными сообщениями — его полдень по UTC, точного времени там нет) и
память разговора (``dialogs.updated_at``). Берётся самый поздний след.
Повторный прогон ничего не меняет: заполняются только пустые отметки.

Revision ID: d1f7b3c9e042
Revises: c9e5a1b7d830
Create Date: 2026-10-16 15:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1f7b3c9e042"
down_revision: str | None = "c9e5a1b7d830"
branch_labels: str | None = None
depends_on: str | None = None

#: Отдельной строкой, чтобы тест мог выполнить ровно её и дважды.
BACKFILL_SQL = (
    "UPDATE users SET last_active_at = GREATEST("
    "users.created_at, "
    "(SELECT max(g.created_at) FROM generations g WHERE g.user_id = users.id), "
    "(SELECT (max(u.day) + interval '12 hours') AT TIME ZONE 'UTC' FROM usage u "
    " WHERE u.user_id = users.id AND u.messages_used > 0), "
    "(SELECT d.updated_at FROM dialogs d WHERE d.user_id = users.id)"
    ") WHERE last_active_at IS NULL"
)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("last_marketing_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_last_active_at", "users", ["last_active_at"])
    op.create_table(
        "mailings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.String(4), nullable=False),
        sa.Column("cycle_key", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "step", "cycle_key", name="uq_mailings_cycle"),
    )
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_table("mailings")
    op.drop_index("ix_users_last_active_at", table_name="users")
    op.drop_column("users", "last_marketing_at")
    op.drop_column("users", "last_active_at")
