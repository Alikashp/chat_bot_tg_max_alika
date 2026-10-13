"""Отметка последней активности в истории переписки (сессия 10, шаг 7).

История без активности 30 дней удаляется сама, а для этого нужно знать,
когда её сохраняли последним. Существующим строкам ставится момент
выкладки: срок у них начинается с неё, а не задним числом.

Revision ID: b8d4f0a6c729
Revises: a6c2e8f4b517
Create Date: 2026-10-13 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f0a6c729"
down_revision: str | None = "a6c2e8f4b517"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dialogs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_dialogs_updated_at", "dialogs", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_dialogs_updated_at", table_name="dialogs")
    op.drop_column("dialogs", "updated_at")
