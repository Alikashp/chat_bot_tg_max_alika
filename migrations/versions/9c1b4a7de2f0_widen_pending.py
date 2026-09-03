"""widen pending

Приколу «я и я в детстве» нужно два фото, а приходят они разными обращениями.
Между ними ссылку на первый снимок помнить негде, кроме как в самом ожидании,
и она туда переехала. В Telegram это file_id под сотню символов, в MAX —
обычный http-адрес, длину которого задаём не мы, — прежних 64 символов не
хватает ни на один из вариантов.

Ширину не угадываем, а снимаем ограничение: рядом по той же причине уже стоит
retry_context типа Text.

Обратная миграция обрезает значения до 64 символов, иначе VARCHAR(64) их не
примет. Обрезанное ожидание разбору не поддастся и будет понято как «ничего не
ждём»: человек в худшем случае увидит список приколов вместо просьбы прислать
второе фото. Терять здесь нечего — ожидание живёт минуты.

Revision ID: 9c1b4a7de2f0
Revises: 3fc16c742044
Create Date: 2026-09-03 12:10:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "9c1b4a7de2f0"
down_revision: str | None = "3fc16c742044"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "pending",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE users SET pending = left(pending, 64) WHERE pending IS NOT NULL")
    op.alter_column(
        "users",
        "pending",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
