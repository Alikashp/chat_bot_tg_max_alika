"""Весь путь звёзд в Telegram без карты (сессия 15, шаг 4, З2 и З6).

Для каждого тарифа: покупка по цене в звёздах, предварительный запрос,
подтверждение, продление, сверка по журналу, отмена и смена тарифа. Формула
цены прежняя. Гоняется и на памяти, и на PostgreSQL: «ровно один раз»
держится на уникальности в базе, а не на коде сценария.
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
from app.core.actions import Action, buy_action
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.reconcile import Reconciler
from app.core.router import handle
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps
from app.core.tariffs import STARS
from app.ports.payments import PaymentMethod, StarPayment, SubscriptionStatus
from tests.fakes import FakeCards, FakeMessenger, FakeStars, FrozenClock

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
#: Цена каждого тарифа в звёздах при настройках по умолчанию.
PRICES = {TariffId.LITE: 262, TariffId.PRO: 525, TariffId.MAX: 1304}
TARIFFS = list(PRICES)


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest, clock: FrozenClock
) -> AsyncIterator[InMemoryStorage | PostgresStorage]:
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
                "TRUNCATE subscriptions, payments, star_ledger, star_cancels, "
                "users RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield PostgresStorage(engine, clock=clock)
    finally:
        await engine.dispose()


@pytest.fixture
def telegram(deps: Deps) -> Deps:
    """Telegram без карты, с ключами магазина и звёздами в продаже."""
    return replace(
        deps,
        cards=FakeCards(recurring=True),
        settings=replace(deps.settings, card_sales_enabled=False, trial_enabled=True),
    )


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def reconciler(deps: Deps) -> Reconciler:
    return Reconciler(
        by_messenger={MessengerKind.TELEGRAM: deps},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    )


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


async def bought(
    deps: Deps, stars: FakeStars, user: User, tariff: TariffId, charge_id: str
) -> str:
    """Покупка через кнопку тарифа: счёт, одобрение, оплата."""
    await handle(deps, press(buy_action(tariff.value)))
    order_id = stars.invoices[-1].order_id
    await payments.approve(deps, f"q-{charge_id}", order_id, user=user)
    assert stars.approvals[-1] == (f"q-{charge_id}", True)
    assert await payments.confirm(deps, order_id, charge_id=charge_id) is not None
    return order_id


@pytest.mark.parametrize("tariff", TARIFFS, ids=lambda each: each.value)
async def test_each_tariff_is_bought_for_its_price_in_stars(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    messenger: FakeMessenger,
    stars: FakeStars,
    tariff: TariffId,
) -> None:
    await handle(telegram, press(buy_action(tariff.value)))

    (invoice,) = stars.invoices
    assert invoice.stars == PRICES[tariff]
    assert f"{PRICES[tariff]} ⭐ каждые 30 дней" in messenger.last_text.text

    await payments.approve(telegram, "q-1", invoice.order_id, user=user)
    await payments.confirm(telegram, invoice.order_id, charge_id="charge-1")

    granted = await fresh(storage, user)
    assert granted.tariff is tariff
    assert granted.tariff_expires_at == telegram.now() + timedelta(days=30)
    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.method == PaymentMethod.STARS.value
    assert subscription.currency == STARS and subscription.amount == PRICES[tariff]
    assert subscription.charge_id == "charge-1"


@pytest.mark.parametrize("tariff", TARIFFS, ids=lambda each: each.value)
async def test_a_renewal_is_granted_once_by_message_and_ledger(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    clock: FrozenClock,
    tariff: TariffId,
) -> None:
    """Продление пришло сообщением, потом его же увидела сверка журнала —
    и сообщение пришло ещё раз. Срок продлён на один период."""
    first = await bought(telegram, stars, user, tariff, "charge-1")
    clock.advance(days=30)

    await payments.confirm(telegram, first, charge_id="charge-2", renewal=True)
    renewed = (await fresh(storage, user)).tariff_expires_at
    stars.transactions = [
        paid(telegram, user, first, "charge-1"),
        paid(telegram, user, first, "charge-2"),
    ]
    clock.advance(minutes=20)
    await reconciler(telegram).run()
    assert (
        await payments.confirm(telegram, first, charge_id="charge-2", renewal=True)
        is None
    )

    found = await fresh(storage, user)
    assert found.tariff is tariff
    assert found.tariff_expires_at == renewed
    assert renewed == telegram.now() - timedelta(minutes=20) + timedelta(days=30)


@pytest.mark.parametrize("tariff", TARIFFS, ids=lambda each: each.value)
async def test_a_renewal_seen_only_in_the_ledger_is_granted_once(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    clock: FrozenClock,
    tariff: TariffId,
) -> None:
    """Сообщение о продлении потерялось — продлевает сверка, и один раз."""
    first = await bought(telegram, stars, user, tariff, "charge-1")
    before = (await fresh(storage, user)).tariff_expires_at
    assert before is not None
    clock.advance(days=30)
    stars.transactions = [
        paid(telegram, user, first, "charge-1"),
        paid(telegram, user, first, "charge-2"),
    ]
    clock.advance(minutes=20)

    await reconciler(telegram).run()
    await reconciler(telegram).run()
    late = await payments.confirm(telegram, first, charge_id="charge-2", renewal=True)

    assert late is None
    found = await fresh(storage, user)
    assert found.tariff is tariff
    # Срок истёк за 20 минут до сверки — продлевается от её момента, один раз.
    assert found.tariff_expires_at == telegram.now() + timedelta(days=30)
    assert found.tariff_expires_at > before


@pytest.mark.parametrize("tariff", TARIFFS, ids=lambda each: each.value)
async def test_cancelling_stops_telegram_and_keeps_the_term(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    tariff: TariffId,
) -> None:
    await bought(telegram, stars, user, tariff, "charge-1")
    term = (await fresh(storage, user)).tariff_expires_at

    await handle(telegram, press(Action.SUBSCRIPTION_OFF))

    assert stars.cancelled == [(user.external_id, "charge-1")]
    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.CANCELLED.value
    found = await fresh(storage, user)
    assert found.tariff is tariff and found.tariff_expires_at == term


@pytest.mark.parametrize(
    ("was", "now"),
    [
        (TariffId.LITE, TariffId.PRO),
        (TariffId.PRO, TariffId.MAX),
        (TariffId.MAX, TariffId.LITE),
    ],
    ids=["lite-pro", "pro-max", "max-lite"],
)
async def test_changing_the_tariff_replaces_the_star_subscription(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    stars: FakeStars,
    was: TariffId,
    now: TariffId,
) -> None:
    """Звёздами на другой тариф Telegram заводит вторую подписку — прежнюю
    мы отменяем у Telegram, чтобы человек не платил за обе."""
    await bought(telegram, stars, user, was, "charge-1")

    await bought(telegram, stars, user, now, "charge-9")

    assert stars.invoices[-1].stars == PRICES[now]
    assert stars.cancelled == [(user.external_id, "charge-1")]
    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.tariff is now and subscription.charge_id == "charge-9"
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert (await fresh(storage, user)).tariff is now
