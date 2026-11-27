"""Нормы пакета на базе после настоящих миграций (сессия 16).

Схема собрана Alembic с нуля, а не ``metadata.create_all``: проверяется то,
что окажется в бою, — колонки норм, их запись выдачей и правило «все четыре
или ни одной».
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.adapters.storage.migrations import _config
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.core import support
from app.core.models import MessengerKind, PackNorms, TariffId
from app.ports.payments import PaymentMethod
from app.ports.storage import GrantOutcome

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
MIGRATION_DB = "botdb_package_norms"
#: Ревизия перед нормами пакета.
BEFORE = "a8d4c6e2f175"


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


async def _migrate(dsn: str, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", dsn)
    step = command.upgrade if action.startswith("up") else command.downgrade
    await asyncio.to_thread(step, _config(dsn), action.split(":", 1)[1])


@pytest.mark.filterwarnings("ignore:No path_separator found:DeprecationWarning")
async def test_package_norms_are_written_with_the_grant_and_cleared_by_a_tariff(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _migrate(dsn, monkeypatch, "up:head")
    await _migrate(dsn, monkeypatch, f"down:{BEFORE}")
    await _migrate(dsn, monkeypatch, "up:head")
    engine = create_engine(dsn)
    storage = PostgresStorage(engine)
    try:
        user = await storage.create_user(
            messenger=MessengerKind.TELEGRAM,
            external_id="1",
            referral_code="code1",
            support_number=support.generate_number(),
            bonus_images=0,
            bonus_documents=0,
        )
        now = datetime.now(UTC)
        norms = PackNorms(messages=200, images=125, documents=94, presentations=63)

        async def order(tariff: TariffId) -> str:
            created = await storage.create_payment(
                user_id=user.id,
                tariff=tariff,
                method=PaymentMethod.STARS.value,
                amount=1490,
                currency="XTR",
                docs_version="2026-08-31",
            )
            return created.id

        first = await order(TariffId.HALFYEAR)
        assert (
            await storage.complete_payment(
                first,
                tariff=TariffId.HALFYEAR,
                expires_at=now + timedelta(days=180),
                seen_tariff=TariffId.FREE,
                seen_expiry=None,
                subscription=None,
                norm_since=now,
                pack=norms,
            )
            is GrantOutcome.GRANTED
        )
        found = await storage.get_user_by_id(user.id)
        assert found is not None and found.pack == norms

        second = await order(TariffId.LITE)
        await storage.complete_payment(
            second,
            tariff=TariffId.LITE,
            expires_at=now + timedelta(days=30),
            seen_tariff=TariffId.HALFYEAR,
            seen_expiry=found.tariff_expires_at,
            subscription=None,
            norm_since=now,
        )
        cleared = await storage.get_user_by_id(user.id)
        assert cleared is not None and cleared.pack is None

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE users SET pack_images = 5 WHERE id = :id"),
                    {"id": int(user.id)},
                )
    finally:
        await engine.dispose()
