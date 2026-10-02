"""Презентации: баланс, отметка о разовой выдаче и слот сборки (фаза 10).

Каждому — одна презентация разово. Новым её выдаёт регистрация, всем, кто
завёлся раньше, — эта миграция.

Выдача идёт по отметке ``presentations_granted_at``, а не по балансу. У
разборов документов отметки не было, и раздачу пришлось делать по «баланс
равен нулю» — отчего те, кто свои уже потратил, получили их второй раз
(см. f8c3d0a91b62). Здесь условие — «отметки ещё нет», и тот же запрос,
выполненный повторно, не найдёт ни одной строки: выдача ровно один раз
(К6).

Порядок шагов не случаен. Колонки добавляются без умолчаний, раздача идёт
по ним, и только после неё умолчания включаются: «одна презентация» и
«выдано сейчас». Умолчания нужны на время выкладки. Старая версия бота,
ещё обслуживающая людей, пока новая поднимается, про эти колонки не знает и
вставляет новых людей без них — с умолчаниями такие люди получают свою
презентацию, а без них остались бы ни с чем.

Обратная миграция удаляет колонки целиком вместе с балансом. Отбирать
подаренное на откате нехорошо, но колонки, на которой баланс лежит, после
отката просто нет; при повторном накате каждый получит одну заново.

Revision ID: c7e4b2a9d150
Revises: f8c3d0a91b62
Create Date: 2026-10-02 12:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c7e4b2a9d150"
down_revision: str | None = "f8c3d0a91b62"
branch_labels: str | None = None
depends_on: str | None = None

#: Разовая выдача тем, кому её ещё не выдавали. Отдельной строкой, чтобы тест
#: мог выполнить ровно её дважды и убедиться, что второй раз ничего не дал.
#: Число в самом запросе, а не настройкой: миграция — снимок решения на своём
#: моменте, и переезжать вслед за настройкой она не должна.
GRANT_SQL = (
    "UPDATE users "
    "SET bonus_presentations = bonus_presentations + 1, "
    "presentations_granted_at = now() "
    "WHERE presentations_granted_at IS NULL"
)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "bonus_presentations", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "presentations_granted_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "users",
        sa.Column("presentation_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_bonus_presentations", "users", "bonus_presentations >= 0"
    )

    op.execute(GRANT_SQL)

    op.alter_column("users", "bonus_presentations", server_default="1")
    op.alter_column(
        "users", "presentations_granted_at", server_default=sa.text("now()")
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_bonus_presentations", "users", type_="check")
    op.drop_column("users", "presentation_started_at")
    op.drop_column("users", "presentations_granted_at")
    op.drop_column("users", "bonus_presentations")
