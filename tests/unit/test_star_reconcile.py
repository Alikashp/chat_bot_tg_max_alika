"""Оплата звёздами не теряется (сессия 10, шаг 1, С1).

Сообщение об оплате звёздами Telegram присылает один раз: ответили 200 —
повтора не будет. Упала обработка или сервис перезапустился, пока сообщение
ждало в очереди, — человек заплатил и остался без тарифа. Сверка читает
журнал транзакций бота с позиции, сохранённой в базе, и доводит каждую оплату
тем же подтверждением, что и сообщение: выдача там однократна.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core import texts
from app.core.models import MessengerKind, TariffId, User
from app.core.reconcile import Reconciler
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, Session
from app.ports.payments import PaymentStatus, StarPayment
from tests.fakes import FakeMessenger, FakeStars, FrozenClock

PRO = TariffId.PRO
TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest, clock: FrozenClock
) -> AsyncIterator[InMemoryStorage | PostgresStorage]:
    """Выдача однократна благодаря базе — значит, и проверять по базе."""
    if request.param == "memory":
        yield InMemoryStorage(clock=clock)
        return
    if TEST_DSN is None:
        pytest.skip("TEST_DATABASE_URL не задан")
    engine = create_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE subscriptions, payments, referrals, dialogs, usage, "
                "monthly_usage, generations, star_ledger, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield PostgresStorage(engine, clock=clock)
    finally:
        await engine.dispose()


async def orders(storage: InMemoryStorage | PostgresStorage) -> int:
    if isinstance(storage, InMemoryStorage):
        return len(storage._payments)
    async with storage._engine.connect() as connection:
        return int(
            (
                await connection.execute(text("SELECT count(*) FROM payments"))
            ).scalar_one()
        )


def reconciler(deps: Deps) -> Reconciler:
    return Reconciler(
        by_messenger={MessengerKind.TELEGRAM: deps},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    )


async def order_for(deps: Deps, session: Session, stars: FakeStars) -> str:
    await payments.start_stars(deps, session, PRO)
    return stars.invoices[-1].order_id


def paid(deps: Deps, user: User, order_id: str, charge_id: str) -> StarPayment:
    return StarPayment(
        charge_id=charge_id,
        user_id=user.external_id,
        order_id=order_id,
        paid_at=deps.now(),
    )


async def fresh(storage: InMemoryStorage | PostgresStorage, user: User) -> User:
    found = await storage.get_user_by_id(user.id)
    assert found is not None
    return found


def broken_once(storage: InMemoryStorage | PostgresStorage) -> None:
    """Выдача падает один раз — как база посреди обработки сообщения."""
    complete = storage.complete_payment
    failed = False

    async def failing(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("база недоступна")
        return await complete(*args, **kwargs)  # type: ignore[arg-type]

    storage.complete_payment = failing  # type: ignore[method-assign,assignment]


async def test_a_payment_whose_handling_failed_is_granted_by_the_pass(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
) -> None:
    order_id = await order_for(deps, session, stars)
    broken_once(storage)
    with pytest.raises(RuntimeError):
        await payments.confirm(deps, order_id, charge_id="charge-1")
    assert (await fresh(storage, session.user)).tariff is TariffId.FREE

    stars.transactions = [paid(deps, session.user, order_id, "charge-1")]
    await reconciler(deps).run()

    granted = await fresh(storage, session.user)
    assert granted.tariff is PRO
    # Второй проход и запоздавшее сообщение второй выдачи не дают.
    await reconciler(deps).run()
    assert await payments.confirm(deps, order_id, charge_id="charge-1") is None
    assert (await fresh(storage, session.user)).tariff_expires_at == (
        granted.tariff_expires_at
    )


async def test_a_payment_lost_in_a_restart_is_granted_and_announced(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    messenger: FakeMessenger,
) -> None:
    """Сообщение об оплате так и не обработано — ушло вместе с процессом."""
    order_id = await order_for(deps, session, stars)
    stars.transactions = [paid(deps, session.user, order_id, "charge-1")]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff is PRO
    assert messenger.last_text.text.startswith(
        f"Готово! Тариф «{texts.TARIFF_TITLES[PRO]}» включён"
    )


async def test_a_lost_renewal_is_granted_once(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    order_id = await order_for(deps, session, stars)
    await payments.confirm(deps, order_id, charge_id="charge-1")
    first = (await fresh(storage, session.user)).tariff_expires_at
    assert first is not None
    clock.advance(days=30)
    stars.transactions = [
        paid(deps, session.user, order_id, "charge-1"),
        paid(deps, session.user, order_id, "charge-2"),
    ]

    await reconciler(deps).run()
    await reconciler(deps).run()
    late = await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)

    assert late is None
    assert (await fresh(storage, session.user)).tariff_expires_at == first + (
        timedelta(days=30)
    )
    renewal = await storage.get_payment_by_external_id("charge-2")
    assert renewal is not None and renewal.status == PaymentStatus.PAID.value


async def test_payments_handled_by_the_message_are_left_alone(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    order_id = await order_for(deps, session, stars)
    await payments.confirm(deps, order_id, charge_id="charge-1")
    clock.advance(days=30)
    await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)
    expires = (await fresh(storage, session.user)).tariff_expires_at
    before = await orders(storage)
    stars.transactions = [
        paid(deps, session.user, order_id, "charge-1"),
        paid(deps, session.user, order_id, "charge-2"),
    ]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff_expires_at == expires
    assert await orders(storage) == before


async def test_a_first_payment_handled_by_an_older_version_is_not_a_renewal(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
) -> None:
    """До сессии 10 первый звёздный заказ не помнил своего списания.

    Его транзакция в журнале не должна выглядеть продлением: это был бы
    второй месяц за одну оплату.
    """
    order_id = await order_for(deps, session, stars)
    await payments.confirm(deps, order_id)
    expires = (await fresh(storage, session.user)).tariff_expires_at
    stars.transactions = [paid(deps, session.user, order_id, "charge-1")]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff_expires_at == expires


async def test_a_renewal_cut_short_after_its_order_is_finished(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    """Заказ продления заведён, выдача упала: сверка доводит его же."""
    order_id = await order_for(deps, session, stars)
    await payments.confirm(deps, order_id, charge_id="charge-1")
    first = (await fresh(storage, session.user)).tariff_expires_at
    assert first is not None
    clock.advance(days=30)
    broken_once(storage)
    with pytest.raises(RuntimeError):
        await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)
    before = await orders(storage)
    stars.transactions = [
        paid(deps, session.user, order_id, "charge-1"),
        paid(deps, session.user, order_id, "charge-2"),
    ]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff_expires_at == first + (
        timedelta(days=30)
    )
    assert await orders(storage) == before


async def test_a_failure_in_the_pass_is_retried_from_the_same_place(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
) -> None:
    """Позиция в журнале не уходит дальше оплаты, которую не довели."""
    order_id = await order_for(deps, session, stars)
    stars.transactions = [None, paid(deps, session.user, order_id, "charge-1")]
    complete = storage.complete_payment

    async def down(*args: object, **kwargs: object) -> object:
        raise RuntimeError("база недоступна")

    storage.complete_payment = down  # type: ignore[method-assign,assignment]
    await reconciler(deps).run()
    assert (await fresh(storage, session.user)).tariff is TariffId.FREE

    storage.complete_payment = complete  # type: ignore[method-assign]
    await reconciler(deps).run()
    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff is PRO
    # Позиция двигается страницами: после сбоя страница читается заново с
    # начала, а доведённое на ней второй раз не выдаётся.
    assert stars.ledger_reads == [0, 0, 2]


async def test_the_ledger_is_read_page_after_page(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
) -> None:
    order_id = await order_for(deps, session, stars)
    stars.transactions = [None] * 250 + [paid(deps, session.user, order_id, "charge-1")]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff is PRO
    assert stars.ledger_reads == [0, 100, 200]
    assert await storage.star_ledger_offset() == 251


async def test_someone_elses_payload_grants_nothing(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
) -> None:
    """Payload приходит из журнала, но платил другой человек — не выдаём."""
    order_id = await order_for(deps, session, stars)
    stranger = replace(paid(deps, session.user, order_id, "charge-1"), user_id="999")
    stars.transactions = [stranger]

    await reconciler(deps).run()

    assert (await fresh(storage, session.user)).tariff is TariffId.FREE
    assert await storage.star_ledger_offset() == 1
