"""Гарантии, которые даёт схема, а не код.

Контрактные тесты проверяют поведение через порт — то есть через наш же
питон. Эти тесты бьют в базу напрямую, в обход приложения, и проверяют, что
инварианты держатся, даже если однажды кто-то напишет запрос мимо адаптера.

Разница принципиальная. «Мы аккуратно проверили self-referral в коде» — это
обещание разработчика. «База физически не примет такую строку» — гарантия.
Критерий приёмки №9 стоит второго, а не первого.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

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
                    "(messenger, external_id, tariff, referral_code, "
                    " support_number, created_at) "
                    "VALUES ('telegram', :ext, 'free', :code, :number, :now) "
                    "RETURNING id"
                ),
                {
                    "ext": str(index),
                    "code": f"code{index}",
                    "number": 100_000 + index,
                    "now": datetime.now(UTC),
                },
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
                    "(messenger, external_id, tariff, referral_code, "
                    " support_number, created_at) "
                    "VALUES ('telegram', '0', 'free', 'another', 900001, :now)"
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
                "(messenger, external_id, tariff, referral_code, "
                " support_number, created_at) "
                "VALUES ('max', '0', 'free', 'max-code', 900003, :now)"
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


# --- Разовая презентация (фаза 10, К6) -----------------------------------


def _migration() -> object:
    """Модуль миграции презентаций: его запрос раздачи и проверяется."""
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "c7e4b2a9d150_presentations.py"
    )
    spec = importlib.util.spec_from_file_location("presentations_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_presentation_grant_runs_once_even_if_repeated(
    engine: AsyncEngine,
) -> None:
    """К6: тот же запрос раздачи, выполненный дважды, даёт одну презентацию.

    Люди, заведённые до миграции, выглядят так: ноль на балансе и нет отметки.
    Кто-то из них свою уже потратил — у него ноль и отметка; ему второй раз
    не положено.
    """
    grant = _migration().GRANT_SQL  # type: ignore[attr-defined]
    old, spent = await _make_users(engine, 2)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE users SET bonus_presentations = 0, "
                "presentations_granted_at = NULL WHERE id = :id"
            ),
            {"id": old},
        )
        await connection.execute(
            text("UPDATE users SET bonus_presentations = 0 WHERE id = :id"),
            {"id": spent},
        )

        await connection.execute(text(grant))
        await connection.execute(text(grant))

        result = await connection.execute(
            text("SELECT id, bonus_presentations FROM users ORDER BY id")
        )
        rows = {int(row[0]): int(row[1]) for row in result.all()}

    assert rows == {old: 1, spent: 0}


async def test_a_row_from_the_old_code_still_gets_its_presentation(
    engine: AsyncEngine,
) -> None:
    """Во время выкладки старый код вставляет людей без новых колонок.

    Умолчания схемы выдают им презентацию и ставят отметку — иначе люди,
    пришедшие в эти минуты, остались бы ни с чем, а раздача их бы пропустила.
    """
    (user_id,) = await _make_users(engine, 1)
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT bonus_presentations, presentations_granted_at "
                    "FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        ).one()

    assert row[0] == 1
    assert row[1] is not None


async def test_presentations_cannot_go_negative(engine: AsyncEngine) -> None:
    (user_id,) = await _make_users(engine, 1)
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE users SET bonus_presentations = -1 WHERE id = :id"),
                {"id": user_id},
            )


# --- Атомарная выдача (фаза 11, П4) --------------------------------------


async def test_a_failure_inside_the_grant_leaves_nothing_half_done(
    engine: AsyncEngine,
) -> None:
    """П4: сбой посреди выдачи — ни заказа paid, ни тарифа, ни подписки.

    Сбой настоящий: подписка с нулевой суммой нарушает ограничение схемы на
    последнем шаге выдачи. Транзакция обязана откатить и первые два шага.
    """
    from datetime import timedelta

    from app.adapters.storage.postgres import PostgresStorage
    from app.core.models import MessengerKind, Subscription, TariffId
    from app.ports.payments import PaymentStatus

    storage = PostgresStorage(engine)
    user = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="grant-fail",
        referral_code="grantfail",
        support_number=654321,
        bonus_images=0,
        bonus_documents=0,
    )
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    now = datetime.now(UTC)
    broken = Subscription(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        status="active",
        amount=0,
        currency="RUB",
        next_charge_at=now + timedelta(days=30),
        created_at=now,
        payment_method_id="card-1",
    )

    with pytest.raises(SQLAlchemyError):
        await storage.complete_payment(
            order.id,
            tariff=TariffId.PRO,
            expires_at=now + timedelta(days=30),
            seen_tariff=user.tariff,
            seen_expiry=user.tariff_expires_at,
            subscription=broken,
            norm_since=now,
        )

    pending = await storage.get_payment(order.id)
    assert pending is not None and pending.status == PaymentStatus.PENDING.value
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.tariff is TariffId.FREE
    assert await storage.get_subscription(user.id) is None


def _forget_topics() -> object:
    """Миграция, стирающая темы презентаций из базы (сессия 7, В6)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "e0a5b7c9d248_forget_presentation_topics.py"
    )
    spec = importlib.util.spec_from_file_location("forget_topics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_old_presentation_topics_leave_the_database(
    engine: AsyncEngine,
) -> None:
    """В6: темы, записанные прежней версией, стираются — и только они."""
    migration = _forget_topics()
    statements = (
        migration.CLEAR_PENDING_SQL,  # type: ignore[attr-defined]
        migration.CLEAR_RETRY_SQL,  # type: ignore[attr-defined]
    )
    ids = await _make_users(engine, 4)
    rows = [
        ("await:pres:theme:Секретная тема", '{"kind":"presentation","prompt":"Тема"}'),
        ("await:pres:from:abc123", None),
        ("await:image", '{"kind":"image","prompt":"кот"}'),
        ("await:pres", '{"kind":"chat","prompt":"привет"}'),
    ]
    async with engine.begin() as connection:
        for user_id, (pending_value, context) in zip(ids, rows, strict=True):
            await connection.execute(
                text(
                    "UPDATE users SET pending = :p, retry_context = :r WHERE id = :id"
                ),
                {"p": pending_value, "r": context, "id": user_id},
            )
        for _ in range(2):
            for statement in statements:
                await connection.execute(text(statement))
        result = await connection.execute(
            text("SELECT pending, retry_context FROM users ORDER BY id")
        )
        found = [tuple(row) for row in result.all()]

    assert found == [
        (None, None),
        (None, None),
        ("await:image", '{"kind":"image","prompt":"кот"}'),
        ("await:pres", '{"kind":"chat","prompt":"привет"}'),
    ]
    assert "Секретная" not in repr(found)
