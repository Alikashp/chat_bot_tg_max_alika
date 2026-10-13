"""Тексты людей уходят из данных для «Повторить» (сессия 9, шаг 0).

``users.retry_context`` держал в поле ``prompt`` сообщение чата при сбое,
описание последней картинки и начало ответа чата для «Нарисуй к этому».
Теперь текст живёт в памяти процесса под жетоном, а в базе — только жетон.

Миграция стирает то, что успела записать прежняя версия:

- у чата без текста повторять нечего — контекст снимается целиком;
- у картинки без готового результата тоже — снимается целиком;
- у картинки с результатом и у остальных видов уходит только поле
  ``prompt``: ссылка на готовую картинку остаётся, и «Поделиться» под
  последней картинкой работает как раньше. «Ещё вариант» под ней честно
  скажет, что повторять нечего.

Запись, которую не разобрать, снимается целиком: прочитать её не может и
сам бот. Повторный прогон ничего не находит — поля ``prompt`` больше нет ни
в одной записи.

Обратной миграции у данных нет: стёртое не вернуть, да и незачем.

Revision ID: f2b8d4a6c391
Revises: b4d6f8a0c253
Create Date: 2026-10-12 12:00:00.000000+00:00
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8d4a6c391"
down_revision: str | None = "b4d6f8a0c253"
branch_labels: str | None = None
depends_on: str | None = None

#: Прежняя версия писала ``prompt`` в каждую запись, даже пустым.
SELECT_SQL = sa.text(
    "SELECT id, retry_context FROM users WHERE retry_context LIKE '%\"prompt\"%'"
)
UPDATE_SQL = sa.text("UPDATE users SET retry_context = :context WHERE id = :id")


def scrub(raw: str) -> str | None:
    """Запись без текста; None — запись снимается целиком."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    if kind == "chat" or (kind == "image" and not payload.get("result_photo")):
        return None
    payload.pop("prompt", None)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def forget(connection: sa.Connection) -> None:
    """Отдельной функцией, чтобы тест мог выполнить ровно её и дважды."""
    rows = connection.execute(SELECT_SQL).all()
    for user_id, raw in rows:
        connection.execute(UPDATE_SQL, {"context": scrub(raw), "id": user_id})


def upgrade() -> None:
    forget(op.get_bind())


def downgrade() -> None:
    """Стёртые тексты не восстанавливаются: их нет нигде, и это и было целью."""
