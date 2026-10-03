"""Месячная норма картинок, докладов и презентаций (фаза 11, часть 2).

Добавляется таблица расхода за период и отметка, с какого момента считается
норма платного тарифа. Данных миграция не трогает: бонусы, накопленные
раньше, остаются ровно такими, какими были (Т4), а расход новой нормы у
всех начинается с нуля — новые нормы действуют сразу после выкладки.

Отметку ``norm_since`` у действующих подписчиков не заполняем: ядро само
отсчитывает их период от конца оплаченного срока назад. Тот же путь нужен
для заказов, выданных старой версией бота в минуты выкладки, — заполнять
её здесь значило бы завести второй способ сделать то же самое.

Поэтому повторить миграцию безопасно: накат после отката снова создаёт
пустую таблицу, а начисленное не задевается ни там, ни там.

Revision ID: a3c9e5f7d218
Revises: e1a7c3d9b204
Create Date: 2026-10-03 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e5f7d218"
down_revision: str | None = "e1a7c3d9b204"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("norm_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "monthly_usage",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("images_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "presentations_used", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.CheckConstraint("images_used >= 0", name="ck_monthly_usage_images"),
        sa.CheckConstraint("documents_used >= 0", name="ck_monthly_usage_documents"),
        sa.CheckConstraint(
            "presentations_used >= 0", name="ck_monthly_usage_presentations"
        ),
    )


def downgrade() -> None:
    op.drop_table("monthly_usage")
    op.drop_column("users", "norm_since")
