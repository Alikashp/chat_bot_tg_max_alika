"""Версия постоянного меню, которую человек видел последней (§4.2).

Постоянная клавиатура Telegram меняется только сообщением, которое её несёт.
Без версии человек, не нажимавший /start, держал бы старое меню сколько
угодно. С версией меню обновляется с первым ответом бота после выкладки —
один раз на версию.

NULL у всех, кто заведён до этой миграции: меню им обновится с первым ответом,
и это ровно то, что нужно после выкладки с новым меню.

Revision ID: d4f1e8b2c6a7
Revises: c7e4b2a9d150
Create Date: 2026-10-05 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d4f1e8b2c6a7"
down_revision: str | None = "c7e4b2a9d150"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("menu_version", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "menu_version")
