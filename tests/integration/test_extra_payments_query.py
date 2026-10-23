"""Запрос лишних оплат из README — на базе после настоящих миграций (О5).

Запрос читается прямо из README: проверяется тот текст, который заказчик
скопирует, а не его пересказ в тесте.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.adapters.storage.migrations import _config
from app.adapters.storage.postgres import create_engine

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
MIGRATION_DB = "botdb_extra_payments"
README = Path(__file__).resolve().parents[2] / "README.md"


def readme_query() -> str:
    blocks = re.findall(r"```sql\n(.*?)```", README.read_text(), flags=re.S)
    (query,) = [block for block in blocks if "Лишние оплаты" in block]
    return str(query)


def test_the_query_only_reads() -> None:
    query = readme_query().upper()
    assert query.lstrip().startswith("--") and "SELECT" in query
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"):
        assert word not in query


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    if TEST_DSN is None:
        pytest.skip("TEST_DATABASE_URL не задан")
    server = create_engine(TEST_DSN)
    admin = server.execution_options(isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        await connection.execute(text(f"CREATE DATABASE {MIGRATION_DB}"))
    base, _, _ = TEST_DSN.rpartition("/")
    try:
        yield f"{base}/{MIGRATION_DB}"
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        await server.dispose()


@pytest.mark.filterwarnings("ignore:No path_separator found:DeprecationWarning")
async def test_the_query_lists_exactly_the_paid_unused_rubles(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", dsn)
    await asyncio.to_thread(command.upgrade, _config(dsn), "head")
    engine = create_engine(dsn)
    at = datetime(2026, 10, 18, 12, tzinfo=UTC)
    rows = [
        # (заказ, человек, пробный, сумма, статус)
        ("granted", 1, True, 1, "paid"),
        ("extra", 1, True, 1, "paid"),
        ("returned", 1, True, 1, "refunded"),
        ("unpaid", 1, True, 1, "pending"),
        ("lite", 2, False, 299, "paid"),
        # Купил Лайт, потом оплатил старую ссылку на 1 ₽: пробного у него нет
        # вовсе, trial_order_id пуст — и эта оплата тоже лишняя.
        ("extra2", 2, True, 1, "paid"),
    ]
    try:
        async with engine.begin() as connection:
            for user_id in (1, 2):
                await connection.execute(
                    text(
                        "INSERT INTO users (id, messenger, external_id, tariff, "
                        "referral_code, support_number, created_at) VALUES "
                        "(:id, 'telegram', :ext, 'lite', :code, :num, :at)"
                    ),
                    {
                        "id": user_id,
                        "ext": str(user_id),
                        "code": f"c{user_id}",
                        "num": 1000 + user_id,
                        "at": at,
                    },
                )
            for order_id, user_id, trial, amount, status in rows:
                await connection.execute(
                    text(
                        "INSERT INTO payments (id, user_id, tariff, method, amount, "
                        "currency, status, created_at, paid_at, external_id, trial) "
                        "VALUES (:id, :u, 'lite', 'card', :amount, 'RUB', :status, "
                        ":at, :at, :ext, :trial)"
                    ),
                    {
                        "id": order_id,
                        "u": user_id,
                        "amount": amount,
                        "status": status,
                        "at": at.replace(minute=len(order_id)),
                        "ext": f"yk-{order_id}",
                        "trial": trial,
                    },
                )
            await connection.execute(
                text("UPDATE users SET trial_order_id = 'granted' WHERE id = 1")
            )

        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            found = (await connection.execute(text(readme_query()))).mappings().all()
    finally:
        await engine.dispose()

    assert [
        (row["user_id"], row["order_id"], row["yookassa_payment_id"], row["amount"])
        for row in found
    ] == [(1, "extra", "yk-extra", 1), (2, "extra2", "yk-extra2", 1)]
    assert found[0]["support_number"] == 1001
