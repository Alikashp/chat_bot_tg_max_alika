"""Накат миграций при старте приложения.

Отдельным шагом деплоя было бы аккуратнее, но на Railway такого шага нет, а
запуск alembic из командной строки в Procfile означал бы, что приложение и
миграции по-разному понимают, где база. Здесь и то, и другое читает одну
переменную и пользуется одной функцией нормализации адреса.

Сервис однопроцессный (§5 задания), поэтому двух одновременных накатов не
бывает. Если инстансов станет больше, это надо будет вынести в отдельный шаг.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

#: Корень репозитория: отсюда берутся alembic.ini и каталог миграций.
_ROOT = Path(__file__).resolve().parents[3]


def _config(dsn: str) -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    # env.py читает DATABASE_URL сам; здесь дублируем адрес для случая, когда
    # миграции вызывают программно с явной строкой.
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def upgrade_to_head(dsn: str) -> None:
    """Накатывает миграции. Блокирующий вызов — Alembic синхронный."""
    command.upgrade(_config(dsn), "head")


async def upgrade_to_head_async(dsn: str) -> None:
    """То же, но не блокируя петлю событий (§3.4.10)."""
    await asyncio.to_thread(upgrade_to_head, dsn)
