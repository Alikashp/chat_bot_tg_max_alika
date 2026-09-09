"""Разовая выдача картинок вместо дневной квоты.

На бесплатном тарифе картинки больше не восстанавливаются каждый день: три
выдаются один раз при регистрации, дальше их добавляют приглашённые друзья и
подписка на канал. Всё это живёт в ``bonus_images``, который не сгорает, а
персональная дневная квота (``daily_image_quota``) становится не нужна.

Уже заведённым людям квота не пропадает молча: тем, кто сидит на бесплатном
тарифе, она переносится в бонус. Иначе выкладка отобрала бы у живого
пользователя картинки, которые он видел в профиле минуту назад. Платных это
не касается: их дневная норма приходит из тарифа и никуда не девается.

Обратная миграция возвращает колонку со значением 3 у всех и бонусы не
отматывает: перенос — разовое решение в пользу пользователя, и отбирать
подаренное на откате было бы хуже, чем оставить.

Revision ID: a7d3f21c9b40
Revises: c4f1a8b7d305
Create Date: 2026-09-09 10:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f21c9b40"
down_revision: str | None = "c4f1a8b7d305"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("channel_bonus_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE users "
        "SET bonus_images = bonus_images + daily_image_quota "
        "WHERE tariff = 'free'"
    )
    op.drop_column("users", "daily_image_quota")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "daily_image_quota",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )
    # Значения по умолчанию у колонки не было: оно нужно было только затем,
    # чтобы заполнить уже существующие строки.
    op.alter_column("users", "daily_image_quota", server_default=None)
    op.drop_column("users", "channel_bonus_at")
