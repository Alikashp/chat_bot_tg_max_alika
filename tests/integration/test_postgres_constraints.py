"""Гарантии, которые даёт схема, а не код.

Контрактные тесты проверяют поведение через порт — то есть через наш же
питон. Эти тесты бьют в базу напрямую, в обход приложения, и проверяют, что
инварианты держатся, даже если однажды кто-то напишет запрос мимо адаптера.

Разница принципиальная. «Мы аккуратно проверили self-referral в коде» — это
обещание разработчика. «База физически не примет такую строку» — гарантия.
Критерий приёмки №9 стоит второго, а не первого.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.storage.postgres import create_engine
from app.adapters.storage.schema import metadata

TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    if TEST_DSN is None:
        pytest.skip("TEST_DATABASE_URL не задан")
    engine = create_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            text("TRUNCATE referrals, dialogs, usage, users RESTART IDENTITY CASCADE")
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _make_users(engine: AsyncEngine, count: int) -> list[int]:
    ids: list[int] = []
    async with engine.begin() as connection:
        for index in range(count):
            row = await connection.execute(
                text(
                    "INSERT INTO users "
                    "(messenger, external_id, tariff, referral_code, created_at, "
                    " daily_image_quota) "
                    "VALUES ('telegram', :ext, 'free', :code, :now, 3) "
                    "RETURNING id"
                ),
                {"ext": str(index), "code": f"code{index}", "now": datetime.now(UTC)},
            )
            ids.append(int(row.scalar_one()))
    return ids


async def test_self_referral_is_impossible_at_the_database_level(
    engine: AsyncEngine,
) -> None:
    """Критерий приёмки №9: не «проверили», а «нельзя»."""
    (user_id,) = await _make_users(engine, 1)

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO referrals (referee_id, referrer_id, created_at) "
                    "VALUES (:id, :id, :now)"
                ),
                {"id": user_id, "now": datetime.now(UTC)},
            )


async def test_one_referee_cannot_be_claimed_twice(engine: AsyncEngine) -> None:
    """Приглашённый приносит награду ровно одному пригласившему."""
    first, second, referee = await _make_users(engine, 3)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO referrals (referee_id, referrer_id, created_at) "
                "VALUES (:referee, :referrer, :now)"
            ),
            {"referee": referee, "referrer": first, "now": datetime.now(UTC)},
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO referrals (referee_id, referrer_id, created_at) "
                    "VALUES (:referee, :referrer, :now)"
                ),
                {"referee": referee, "referrer": second, "now": datetime.now(UTC)},
            )


async def test_bonus_cannot_go_negative(engine: AsyncEngine) -> None:
    """Даже прямой запрос мимо приложения не уведёт баланс в минус."""
    (user_id,) = await _make_users(engine, 1)

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE users SET bonus_images = -1 WHERE id = :id"),
                {"id": user_id},
            )


async def test_same_external_id_twice_in_one_messenger_is_impossible(
    engine: AsyncEngine,
) -> None:
    await _make_users(engine, 1)

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users "
                    "(messenger, external_id, tariff, referral_code, created_at, "
                    " daily_image_quota) "
                    "VALUES ('telegram', '0', 'free', 'another', :now, 3)"
                ),
                {"now": datetime.now(UTC)},
            )


async def test_the_same_id_in_another_messenger_is_a_different_person(
    engine: AsyncEngine,
) -> None:
    """Один и тот же числовой id в Telegram и в MAX — разные люди."""
    await _make_users(engine, 1)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users "
                "(messenger, external_id, tariff, referral_code, created_at, "
                " daily_image_quota) "
                "VALUES ('max', '0', 'free', 'max-code', :now, 3)"
            ),
            {"now": datetime.now(UTC)},
        )
        total = await connection.execute(text("SELECT count(*) FROM users"))

    assert total.scalar_one() == 2


async def test_database_errors_never_carry_the_conversation(
    engine: AsyncEngine,
) -> None:
    """§3.5: в логах не должно быть содержимого сообщений.

    Текст ошибки SQLAlchemy по умолчанию включает запрос вместе с
    параметрами, а среди параметров — переписка. Ошибка базы рано или поздно
    попадает в лог, поэтому параметры отключены на уровне движка.
    """
    secret = "совершенно личная переписка"

    with pytest.raises(SQLAlchemyError) as failure:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO dialogs (user_id, turns) VALUES (:id, :turns)"),
                {"id": 10**9, "turns": secret},
            )

    assert secret not in str(failure.value)
    assert secret not in repr(failure.value)
