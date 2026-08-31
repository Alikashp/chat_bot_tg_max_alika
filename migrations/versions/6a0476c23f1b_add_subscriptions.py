"""add subscriptions

Revision ID: 6a0476c23f1b
Revises: ba58a236f719
Create Date: 2026-08-31 13:30:29.451316+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "6a0476c23f1b"
down_revision: str | None = "ba58a236f719"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tariff", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("next_charge_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_method_id", sa.String(length=128), nullable=True),
        sa.Column("charge_id", sa.String(length=128), nullable=True),
        sa.Column("reminded_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_checked_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_subscriptions_amount"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        op.f("ix_subscriptions_next_charge_at"),
        "subscriptions",
        ["next_charge_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_subscriptions_next_charge_at"), table_name="subscriptions")
    op.drop_table("subscriptions")
