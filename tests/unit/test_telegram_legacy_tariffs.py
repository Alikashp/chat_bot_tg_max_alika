"""Прежние тарифы в Telegram после пакетов (сессия 16, шаг 4, П6, П7).

Новые подписки Лайт, Про, Макс за звёзды не продаются: старые кнопки из
переписок ведут на экран пакетов, а счёт на подписку, выданный раньше, не
одобряется. Кто уже на Лайте, Про или Максе — звёздами или картой, —
дорабатывает срок по прежним правилам; продление действующей звёздной
подписки, если Telegram его спишет, засчитывается ровно один раз.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import (
    TRIAL_TARGET,
    Action,
    buy_action,
    email_action,
    method_action,
)
from app.core.billing import Billing
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
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps
from app.core.tariffs import RUB, STARS
from app.ports.payments import (
    PaymentMethod,
    PaymentStatus,
    StarPayment,
    SubscriptionStatus,
)
from tests.fakes import FakeCards, FakeMessenger, FakeStars, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
PACKAGES_TITLE = texts.PACKAGES_TITLE


@pytest.fixture
def cards() -> FakeCards:
    """Ключи магазина Telegram на месте — худший случай для «не к карте»."""
    found = FakeCards(recurring=True)
    found.paid = False
    return found


@pytest.fixture
def telegram(deps: Deps, cards: FakeCards) -> Deps:
    return replace(
        deps,
        cards=cards,
        settings=replace(
            deps.settings,
            card_sales_enabled=False,
            packages_enabled=True,
            trial_enabled=True,
        ),
    )


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def seen(messenger: FakeMessenger) -> str:
    parts: list[str] = []
    for sent in messenger.texts:
        parts.append(sent.text)
        if sent.keyboard is not None:
            parts.extend(button.text for row in sent.keyboard.rows for button in row)
    return "\n".join(parts)


def sells_nothing_but_packages(
    messenger: FakeMessenger, cards: FakeCards, stars: FakeStars
) -> None:
    shown = seen(messenger)
    assert cards.created == [], "платёж ЮKassa не создаётся"
    assert all(invoice.period_days is None for invoice in stars.invoices)
    for word in ("₽", "/мес", "каждые", "💳 Картой", "Лайт", "Про —", "Макс —"):
        assert word not in shown, (word, shown)


@pytest.mark.parametrize(
    "action",
    [
        buy_action("lite"),
        buy_action("pro"),
        buy_action("max"),
        method_action("stars", "lite"),
        method_action("stars", "max"),
        method_action("card", "pro"),
        Action.PAYWALL_TRIAL,
        Action.TRIAL,
        email_action("pro"),
        email_action(TRIAL_TARGET),
    ],
    ids=[
        "lite",
        "pro",
        "max",
        "stars-lite",
        "stars-max",
        "card-pro",
        "paywall-trial",
        "trial",
        "email",
        "trial-email",
    ],
)
async def test_an_old_tariff_button_leads_to_the_packages(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
    action: str,
) -> None:
    await handle(telegram, press(action))

    assert messenger.last_text.text.startswith(PACKAGES_TITLE)
    assert stars.invoices == []
    sells_nothing_but_packages(messenger, cards, stars)


@pytest.mark.parametrize(
    "action",
    [Action.MENU_TARIFFS, Action.OPEN_TARIFFS, Action.MENU_PROFILE],
)
async def test_the_main_screens_sell_no_subscription(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
    action: str,
) -> None:
    await handle(telegram, press(action))

    sells_nothing_but_packages(messenger, cards, stars)


# --- Перед оплатой -----------------------------------------------------------


async def test_an_unpaid_subscription_invoice_from_before_is_refused(
    telegram: Deps, user: User, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Счёт на звёздный Лайт выдан до пакетов и не оплачен: Telegram
    спрашивает перед оплатой — отказываем, иначе завелась бы новая подписка."""
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method=PaymentMethod.STARS.value,
        amount=262,
        currency=STARS,
        docs_version="2026-08-31",
    )

    await payments.approve(telegram, "q-1", order.id, user=user)

    assert stars.approvals == [("q-1", False)]


async def test_an_unpaid_package_invoice_is_approved(
    telegram: Deps, user: User, stars: FakeStars
) -> None:
    await handle(telegram, press(buy_action("week")))

    await payments.approve(telegram, "q-1", stars.invoices[0].order_id, user=user)

    assert stars.approvals == [("q-1", True)]


# --- Действующая звёздная подписка дорабатывает --------------------------------


async def live_star_lite(deps: Deps, user: User) -> str:
    """Звёздный Лайт, оплаченный до пакетов: заказ и живая подписка."""
    order = await deps.storage.create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method=PaymentMethod.STARS.value,
        amount=262,
        currency=STARS,
        docs_version="2026-08-31",
    )
    await deps.storage.attach_external_id(order.id, "charge-1")
    assert await deps.storage.mark_paid(order.id)
    ends = deps.now() + timedelta(days=1)
    await deps.storage.save_subscription(
        Subscription(
            user_id=user.id,
            tariff=TariffId.LITE,
            method=PaymentMethod.STARS.value,
            status=SubscriptionStatus.ACTIVE.value,
            amount=262,
            currency=STARS,
            next_charge_at=ends,
            created_at=deps.now() - timedelta(days=29),
            charge_id="charge-1",
        )
    )
    await deps.storage.set_tariff(user.id, TariffId.LITE, ends)
    return order.id


async def test_a_telegram_renewal_of_a_live_star_lite_is_approved_and_counted_once(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    first = await live_star_lite(telegram, user)
    ends = (await storage.get_user_by_id(user.id)).tariff_expires_at  # type: ignore[union-attr]
    assert ends is not None

    await payments.approve(telegram, "q-renew", first, user=user)
    assert stars.approvals == [("q-renew", True)]

    paid = IncomingMessage(
        chat=CHAT,
        external_user_id="1",
        paid_order_id=first,
        paid_charge_id="charge-2",
        paid_renewal=True,
    )
    await handle(telegram, paid)
    await handle(telegram, paid)
    stars.transactions = [
        StarPayment(charge_id="charge-2", user_id="1", order_id=first, paid_at=ends)
    ]
    clock.advance(minutes=20)
    await Reconciler(
        by_messenger={MessengerKind.TELEGRAM: telegram},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    ).run()

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.tariff is TariffId.LITE
    assert found.tariff_expires_at == ends + timedelta(days=30)
    renewals = [
        order
        for order in storage.orders_of(user.id)
        if order.status == PaymentStatus.PAID.value
    ]
    assert len(renewals) == 2, "первая оплата и ровно одно продление"


async def test_cancelling_a_live_star_lite_does_not_offer_it_back(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
) -> None:
    await live_star_lite(telegram, user)

    await handle(telegram, press(Action.SUBSCRIPTION_OFF))
    await handle(telegram, press(Action.SUBSCRIPTION))

    assert stars.cancelled == [(user.external_id, "charge-1")]
    shown = seen(messenger)
    assert "Вернуть подписку" not in shown and "Вернуть можно" not in shown
    assert "пакеты" in messenger.last_text.text


# --- Действующая карточная подписка дорабатывает --------------------------------


async def card_lite(deps: Deps, user: User, *, ends_in: timedelta) -> None:
    ends = deps.now() + ends_in
    await deps.storage.save_subscription(
        Subscription(
            user_id=user.id,
            tariff=TariffId.LITE,
            method=PaymentMethod.CARD.value,
            status=SubscriptionStatus.ACTIVE.value,
            amount=299,
            currency=RUB,
            next_charge_at=ends,
            created_at=deps.now() - timedelta(days=20),
            payment_method_id="card-1",
        )
    )
    await deps.storage.set_tariff(user.id, TariffId.LITE, ends)


async def test_a_card_lite_works_out_its_term_and_points_to_packages(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await card_lite(telegram, user, ends_in=timedelta(hours=12))

    await handle(telegram, press(Action.MENU_PROFILE))
    assert messenger.last_text.text.startswith("Твой тариф: Лайт · до ")
    await handle(telegram, press(Action.SUBSCRIPTION))
    sells_nothing_but_packages_but_lite(messenger, cards, stars)

    await Billing(by_messenger={MessengerKind.TELEGRAM: telegram}).run()
    notice = messenger.last_text
    assert "закончится тариф «Лайт»" in notice.text
    assert "⭐ за 30 дней" not in notice.text
    assert notice.keyboard is not None
    assert notice.keyboard.rows[0][0].action == Action.OPEN_TARIFFS

    clock.advance(hours=13)
    await Billing(by_messenger={MessengerKind.TELEGRAM: telegram}).run()
    assert cards.charged == []


def sells_nothing_but_packages_but_lite(
    messenger: FakeMessenger, cards: FakeCards, stars: FakeStars
) -> None:
    """Экран подписки называет Лайт — свой, действующий, — но не продаёт его."""
    shown = seen(messenger)
    assert cards.created == [] and stars.invoices == []
    assert "Продлить за" not in shown and "₽" not in shown
