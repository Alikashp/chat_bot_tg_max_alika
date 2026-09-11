"""Источник регистрации и учёт обращений к провайдерам.

Две независимые вещи в одной миграции: обе про наблюдаемость и обе выкатятся
вместе.

``users.source`` — откуда человек пришёл: payload из ссылки `?start=...`, а
если ссылки не было — слово «direct». Уже заведённым людям источник задним
числом не восстановить, и они получают «direct» значением по умолчанию: это
честнее пустой ячейки, которая одинаково читается и как «пришёл сам», и как
«мы не записали».

``generations`` — по строке на каждое обращение к провайдеру, включая
упавшие. Упавшие нужны не для полноты: провайдер берёт деньги за попытку, и
без них доля брака видна только по счёту в конце месяца. Содержимого запросов
и ответов в таблице нет и быть не должно (§3.5).

Revision ID: b2e9c4a1f7d8
Revises: a7d3f21c9b40
Create Date: 2026-09-11 10:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2e9c4a1f7d8"
down_revision: str | None = "a7d3f21c9b40"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default="direct",
        ),
    )
    op.create_table(
        "generations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("preset_id", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_generations_duration"),
    )
    # Два разреза, которые спрашивают на самом деле: «что делал этот человек»
    # и «что происходило за такой-то период».
    op.create_index(
        "ix_generations_user_created", "generations", ["user_id", "created_at"]
    )
    op.create_index("ix_generations_created", "generations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_generations_created", table_name="generations")
    op.drop_index("ix_generations_user_created", table_name="generations")
    op.drop_table("generations")
    op.drop_column("users", "source")
