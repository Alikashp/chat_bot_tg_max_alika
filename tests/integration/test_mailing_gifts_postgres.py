"""Подарок из R2 и отметка «доставлено» на базе после настоящих миграций (Д2).

Схема здесь собрана Alembic с нуля, а не ``metadata.create_all``: проверяется
то, что окажется в бою, — уникальность подарка на отправку и связь с ней.
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
from app.core.gifts import GiftGrant
from app.core.limits import LimitKind
from app.core.models import MailingStatus, MessengerKind

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
MIGRATION_DB = "botdb_mailing_gifts"
#: Ревизия перед связью подарка с отправкой.
BEFORE = "e4a2c8f0d153"


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
async def test_a_delivered_r2_and_its_gift_are_one_record(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _migrate(dsn, monkeypatch, "up:head")
    # Откат и накат снова: ревизия обратима и повторяема.
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
        gift = GiftGrant(
            kind=LimitKind.PRESENTATIONS,
            source="R2",
            expires_at=now + timedelta(days=3),
        )
        await storage.claim_mailing(user.id, "R2", now, now)

        done = await asyncio.gather(
            *(
                storage.finish_mailing(
                    user.id, "R2", now, status=MailingStatus.SENT, at=now, gift=gift
                )
                for _ in range(3)
            )
        )

        assert sorted(done) == [False, False, True]
        async with engine.begin() as connection:
            linked = (
                await connection.execute(
                    text(
                        "SELECT g.mailing_id = m.id FROM gifts g "
                        "JOIN mailings m ON m.user_id = g.user_id"
                    )
                )
            ).all()
        assert [tuple(row) for row in linked] == [(True,)]

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO gifts (user_id, kind, source, granted_at, "
                        "expires_at, mailing_id) SELECT user_id, kind, source, "
                        "granted_at, expires_at, mailing_id FROM gifts"
                    )
                )
    finally:
        await engine.dispose()
