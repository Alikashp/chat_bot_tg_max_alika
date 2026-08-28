"""Окружение Alembic.

Строка подключения берётся из DATABASE_URL и приводится к драйверу asyncpg
той же функцией, что и в приложении: одна логика на два места означала бы,
что миграции и приложение однажды разойдутся в понимании одного и того же
адреса.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.adapters.storage.postgres import normalise_dsn
from app.adapters.storage.schema import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL не задан — миграциям некуда применяться")
    return normalise_dsn(dsn)


def run_migrations_offline() -> None:
    """Печатает SQL, не подключаясь к базе."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _apply(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Применяет миграции к базе."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=NullPool)

    async with engine.connect() as connection:
        await connection.run_sync(_apply)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
