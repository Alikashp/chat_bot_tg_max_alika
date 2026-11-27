"""Покупка пакета звёздами (сессия 16, шаг 2, П2, П3, П5).

Счёт разовый, без периода подписки. Выдача — ровно одна: повтор сообщения
об оплате, сверка журнала звёзд и перезапуск второй не дают. Покупка поверх
действующего пакета складывает остатки, берёт позднюю дату и большую
дневную норму сообщений. Гоняется на памяти и на PostgreSQL: «ровно один
раз» держит база.
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
from app.core.actions import buy_action
from app.core.limits import LimitKind
from app.core.models import (
    Chat,
    IncomingMessage,
    MessengerKind,
    Subscription,
    TariffId,
    User,
)
from app.core.reconcile import Reconciler
from app.core.router import handle
from app.core.scenarios import payments, spending
from app.core.scenarios.deps import Deps, session_for
from app.core.tariffs import STARS
from app.ports.payments import PaymentMethod, StarPayment, SubscriptionStatus
from app.ports.storage import Storage as StoragePort
from tests.fakes import FakeMessenger, FakeStars, FrozenClock

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
PREMIUM_BOT = "https://t.me/PremiumBot"

Storage = InMemoryStorage | PostgresStorage


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest, clock: FrozenClock
) -> AsyncIterator[Storage]:
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
                "monthly_usage, users RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield PostgresStorage(engine, clock=clock)
    finally:
        await engine.dispose()


@pytest.fixture
def telegram(deps: Deps) -> Deps:
    """Telegram с пакетами: карты нет, звёзды продаются."""
    return replace(
        deps,
        cards=None,
        settings=replace(
            deps.settings, card_sales_enabled=False, packages_enabled=True
        ),
    )


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def paid_message(order_id: str, charge_id: str) -> IncomingMessage:
    return IncomingMessage(
        chat=CHAT,
        external_user_id="1",
        paid_order_id=order_id,
        paid_charge_id=charge_id,
    )


async def fresh(storage: StoragePort, user: User) -> User:
    found = await storage.get_user_by_id(user.id)
    assert found is not None
    return found


async def buy(deps: Deps, stars: FakeStars, package: TariffId, charge_id: str) -> str:
    """Кнопка покупки, одобрение перед оплатой и сообщение об оплате."""
    await handle(deps, press(buy_action(package.value)))
    order_id = stars.invoices[-1].order_id
    user = await deps.storage.get_user(MessengerKind.TELEGRAM, "1")
    await payments.approve(deps, f"q-{charge_id}", order_id, user=user)
    assert stars.approvals[-1] == (f"q-{charge_id}", True)
    await handle(deps, paid_message(order_id, charge_id))
    return order_id


async def left(deps: Deps, user: User) -> tuple[int, int, int, int]:
    """Остаток нормы пакета: сообщения в день, картинки, презентации, доклады."""
    session = session_for(deps, await fresh(deps.storage, user))
    found = [
        await spending.current_allowance(deps, session, kind)
        for kind in (
            LimitKind.MESSAGES,
            LimitKind.IMAGES,
            LimitKind.PRESENTATIONS,
            LimitKind.DOCUMENTS,
        )
    ]
    return (
        found[0].daily_limit,
        found[1].monthly_left,
        found[2].monthly_left,
        found[3].monthly_left,
    )


async def spend(deps: Deps, user: User, kind: LimitKind, count: int) -> None:
    session = session_for(deps, await fresh(deps.storage, user))
    start = spending.period_of(deps, session).start
    for _ in range(count):
        assert await deps.storage.spend_norm(user.id, start, kind, limit=1000)


# --- Счёт ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("package", "price"),
    [(TariffId.WEEK, 149), (TariffId.MONTH, 349), (TariffId.HALFYEAR, 1490)],
    ids=["week", "month", "halfyear"],
)
async def test_the_invoice_is_one_off_and_points_to_premium_bot(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    stars: FakeStars,
    package: TariffId,
    price: int,
) -> None:
    await handle(telegram, press(buy_action(package.value)))

    (invoice,) = stars.invoices
    assert invoice.stars == price
    assert invoice.period_days is None, "без периода подписки"
    assert "@PremiumBot" in invoice.description
    assert "российской карты" in invoice.description

    sent = messenger.last_text
    assert "@PremiumBot" in sent.text
    assert "каждые" not in sent.text and "Подписка" not in sent.text
    assert sent.keyboard is not None
    buttons = [button for row in sent.keyboard.rows for button in row]
    assert buttons[0].url == f"https://t.me/invoice/{invoice.order_id}"
    (premium,) = [b for b in buttons if b.text == "Приобрести звёзды 💳"]
    assert premium.url == PREMIUM_BOT


# --- Выдача ровно один раз -------------------------------------------------


@pytest.mark.parametrize(
    ("package", "numbers", "days"),
    [
        (TariffId.WEEK, (100, 5, 3, 5), 7),
        (TariffId.MONTH, (100, 20, 10, 15), 30),
        (TariffId.HALFYEAR, (200, 120, 60, 90), 180),
    ],
    ids=["week", "month", "halfyear"],
)
async def test_a_package_is_granted_without_a_subscription(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    package: TariffId,
    numbers: tuple[int, int, int, int],
    days: int,
) -> None:
    await buy(telegram, stars, package, "charge-1")

    found = await fresh(storage, user)
    assert found.tariff is package
    assert found.tariff_expires_at == telegram.now() + timedelta(days=days)
    assert await storage.get_subscription(user.id) is None
    assert await left(telegram, user) == numbers


async def test_a_repeated_message_the_ledger_and_a_restart_grant_once(
    telegram: Deps,
    user: User,
    storage: Storage,
    messenger: FakeMessenger,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    order_id = await buy(telegram, stars, TariffId.WEEK, "charge-1")
    granted = await fresh(storage, user)

    await handle(telegram, paid_message(order_id, "charge-1"))
    stars.transactions = [
        StarPayment(
            charge_id="charge-1",
            user_id="1",
            order_id=order_id,
            paid_at=telegram.now(),
        )
    ]
    clock.advance(minutes=20)
    reconciler = Reconciler(
        by_messenger={MessengerKind.TELEGRAM: telegram},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    )
    await reconciler.run()
    # Худший перезапуск: позиция журнала потеряна, и его читают с начала —
    # новым процессом, с новым экземпляром сверки.
    await storage.set_star_ledger_offset(0)
    await Reconciler(
        by_messenger={MessengerKind.TELEGRAM: telegram},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    ).run()
    await handle(telegram, paid_message(order_id, "charge-1"))

    found = await fresh(storage, user)
    assert found.tariff_expires_at == granted.tariff_expires_at
    assert await left(telegram, user) == (100, 5, 3, 5)
    done = [sent for sent in messenger.texts if sent.text.startswith("Готово")]
    assert len(done) == 1


async def test_a_payment_whose_message_was_lost_is_granted_by_the_ledger_once(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await handle(telegram, press(buy_action("month")))
    order_id = stars.invoices[-1].order_id
    stars.transactions = [
        StarPayment(
            charge_id="charge-1",
            user_id="1",
            order_id=order_id,
            paid_at=telegram.now(),
        )
    ]
    clock.advance(minutes=20)
    reconciler = Reconciler(
        by_messenger={MessengerKind.TELEGRAM: telegram},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    )

    await reconciler.run()
    await reconciler.run()
    late = await payments.confirm(telegram, order_id, charge_id="charge-1")

    assert late is None
    found = await fresh(storage, user)
    assert found.tariff is TariffId.MONTH
    assert found.tariff_expires_at == telegram.now() + timedelta(days=30)
    assert await left(telegram, user) == (100, 20, 10, 15)


async def test_a_paid_package_link_opened_again_is_refused(
    telegram: Deps, user: User, stars: FakeStars
) -> None:
    """Разовый счёт, оплаченный однажды, второй раз не одобряется: пакет по
    нему уже выдан, и вторая оплата ушла бы без пакета."""
    order_id = await buy(telegram, stars, TariffId.WEEK, "charge-1")
    found = await telegram.storage.get_user(MessengerKind.TELEGRAM, "1")

    await payments.approve(telegram, "q-again", order_id, user=found)

    assert stars.approvals[-1] == ("q-again", False)


# --- Покупка поверх действующего пакета ------------------------------------


async def test_leftovers_add_up_and_the_later_date_wins(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    """Месяц, потрачено 7 картинок и 2 презентации; через 10 дней — Неделя.
    Остатки Месяца + Неделя целиком, срок — конец Месяца."""
    await buy(telegram, stars, TariffId.MONTH, "charge-1")
    month_ends = (await fresh(storage, user)).tariff_expires_at
    await spend(telegram, user, LimitKind.IMAGES, 7)
    await spend(telegram, user, LimitKind.PRESENTATIONS, 2)
    clock.advance(days=10)

    await buy(telegram, stars, TariffId.WEEK, "charge-2")

    found = await fresh(storage, user)
    assert found.tariff_expires_at == month_ends
    assert found.tariff is TariffId.MONTH, "называется тот, что кончается позже"
    assert await left(telegram, user) == (100, 13 + 5, 8 + 3, 15 + 5)


async def test_a_bigger_package_on_top_takes_its_date_and_daily_norm(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await buy(telegram, stars, TariffId.WEEK, "charge-1")
    await spend(telegram, user, LimitKind.DOCUMENTS, 1)
    clock.advance(days=2)

    await buy(telegram, stars, TariffId.HALFYEAR, "charge-2")

    found = await fresh(storage, user)
    assert found.tariff is TariffId.HALFYEAR
    assert found.tariff_expires_at == telegram.now() + timedelta(days=180)
    assert await left(telegram, user) == (200, 5 + 120, 3 + 60, 4 + 90)


async def test_a_smaller_package_keeps_the_bigger_daily_norm(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await buy(telegram, stars, TariffId.HALFYEAR, "charge-1")
    clock.advance(days=1)

    await buy(telegram, stars, TariffId.WEEK, "charge-2")

    daily, *_ = await left(telegram, user)
    assert daily == 200


async def test_an_ended_package_leaves_nothing_to_add(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await buy(telegram, stars, TariffId.WEEK, "charge-1")
    clock.advance(days=8)

    await buy(telegram, stars, TariffId.WEEK, "charge-2")

    found = await fresh(storage, user)
    assert found.tariff_expires_at == telegram.now() + timedelta(days=7)
    assert await left(telegram, user) == (100, 5, 3, 5)


async def test_the_package_keeps_the_bonus_apart(
    telegram: Deps, user: User, storage: Storage, stars: FakeStars
) -> None:
    """Бонусы за друзей не смешиваются с пакетом и не сгорают с ним."""
    before = (await fresh(storage, user)).bonus_images

    await buy(telegram, stars, TariffId.WEEK, "charge-1")

    session = session_for(telegram, await fresh(storage, user))
    images = await spending.current_allowance(telegram, session, LimitKind.IMAGES)
    assert images.bonus == before
    assert images.total_left == 5 + before


async def test_a_package_on_top_of_a_star_subscription_stops_it(
    telegram: Deps,
    user: User,
    storage: Storage,
    stars: FakeStars,
) -> None:
    """Действующий звёздный Лайт: пакет его заменяет, остаток Лайта
    складывается с пакетом, а Telegram больше не спишет за Лайт."""
    now = telegram.now()
    await storage.save_subscription(
        Subscription(
            user_id=user.id,
            tariff=TariffId.LITE,
            method=PaymentMethod.STARS.value,
            status=SubscriptionStatus.ACTIVE.value,
            amount=262,
            currency=STARS,
            next_charge_at=now + timedelta(days=20),
            created_at=now - timedelta(days=10),
            charge_id="lite-charge",
        )
    )
    await storage.set_tariff(user.id, TariffId.LITE, now + timedelta(days=20))

    await buy(telegram, stars, TariffId.WEEK, "charge-1")

    assert stars.cancelled == [(user.external_id, "lite-charge")]
    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.CANCELLED.value
    found = await fresh(storage, user)
    assert found.tariff_expires_at == now + timedelta(days=20)
    _, images, _, _ = await left(telegram, user)
    assert images == 20 + 5


@pytest.mark.parametrize("method", ["stars", "card"])
async def test_a_forged_method_button_does_not_make_a_package_a_subscription(
    telegram: Deps, user: User, stars: FakeStars, method: str
) -> None:
    """Данные кнопки приходят снаружи: «звёздами» или «картой» на пакет —
    всё равно разовый счёт, а не подписка."""
    from app.core.actions import method_action

    await handle(telegram, press(method_action(method, "week")))

    assert [invoice.period_days for invoice in stars.invoices] == [None]


async def test_a_forged_package_button_in_max_starts_no_card_payment(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """В MAX пакетов нет: кнопка пакета с карточным способом не заводит
    карточный заказ, а показывает обычные тарифы."""
    from app.core.actions import method_action
    from tests.fakes import FakeCards

    cards = FakeCards(recurring=True)
    in_max = replace(deps, cards=cards, stars=None)

    for action in (method_action("card", "week"), buy_action("week")):
        await handle(in_max, press(action))

    assert cards.created == []
    assert "299 ₽/мес" in messenger.last_text.text
