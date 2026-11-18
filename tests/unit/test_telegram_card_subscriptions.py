"""Карточные подписки Telegram после выключения карты (сессия 15, шаг 2, З3).

С карт в Telegram больше ничего не списывается: ни продление, ни первые
299 ₽ после пробного периода, ни сроки, накопившиеся, пока ключей магазина не
было. Оплаченный срок дорабатывает, а за сутки до конца человек узнаёт, что
сам тариф не продлится и что продлить можно звёздами.

Ключи магазина Telegram здесь на месте и с автоплатежами: это тот день, когда
их вернут, и худший случай — прежний код списал бы со всех.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import buy_action
from app.core.billing import Billing
from app.core.models import MessengerKind, Subscription, TariffId, User
from app.core.scenarios.deps import Deps
from app.core.tariffs import RUB
from app.ports.payments import PaymentMethod, SubscriptionStatus
from tests.fakes import FakeCards, FakeMessenger, FakeStars, FrozenClock

LITE = TariffId.LITE


@pytest.fixture
def cards() -> FakeCards:
    return FakeCards(recurring=True)


@pytest.fixture
def telegram(deps: Deps, cards: FakeCards) -> Deps:
    return replace(
        deps,
        cards=cards,
        settings=replace(deps.settings, card_sales_enabled=False, trial_enabled=True),
    )


def billing(deps: Deps) -> Billing:
    return Billing(by_messenger={MessengerKind.TELEGRAM: deps})


async def subscribe(
    deps: Deps,
    user: User,
    *,
    ends_in: timedelta,
    trial: bool = False,
    status: str = SubscriptionStatus.ACTIVE.value,
    charge_order_id: str | None = None,
    amount: int = 299,
) -> Subscription:
    """Карточная подписка «Лайт», срок которой кончается через ``ends_in``."""
    ends = deps.now() + ends_in
    subscription = Subscription(
        user_id=user.id,
        tariff=LITE,
        method=PaymentMethod.CARD.value,
        status=status,
        amount=amount,
        currency=RUB,
        next_charge_at=ends,
        created_at=deps.now() - timedelta(days=30),
        payment_method_id="card-1",
        remind_before_charge=trial,
        failed_since=(
            deps.now() - timedelta(days=1)
            if status == SubscriptionStatus.PAST_DUE.value
            else None
        ),
    )
    await deps.storage.save_subscription(subscription)
    if charge_order_id is not None:
        await deps.storage.hold_charge_order(user.id, charge_order_id)
    await deps.storage.set_tariff(user.id, LITE, ends)
    return subscription


async def state(storage: InMemoryStorage, user: User) -> Subscription:
    found = await storage.get_subscription(user.id)
    assert found is not None
    return found


def no_charge_promise(messenger: FakeMessenger) -> None:
    for sent in messenger.texts:
        assert "₽" not in sent.text, sent.text
        assert "спишем" not in sent.text, sent.text


@pytest.mark.parametrize("trial", [False, True], ids=["renewal", "after-trial"])
async def test_a_due_card_subscription_is_not_charged(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    trial: bool,
) -> None:
    """Ни продление, ни первые 299 ₽ после пробного периода."""
    await subscribe(telegram, user, ends_in=-timedelta(minutes=1), trial=trial)

    await billing(telegram).run()

    assert cards.charged == []
    assert (await state(storage, user)).status == SubscriptionStatus.CANCELLED.value
    no_charge_promise(messenger)


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PAST_DUE.value],
)
async def test_terms_piled_up_without_keys_are_not_charged_when_keys_return(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    status: str,
) -> None:
    """Неделю ключей не было: проходы ждали магазин. Ключи вернули —
    списаний нет, человеку одно сообщение, и подписка закрыта."""
    await subscribe(telegram, user, ends_in=-timedelta(days=7), status=status)
    no_keys = replace(telegram, cards=None)
    await billing(no_keys).run()

    await billing(telegram).run()
    clock.advance(days=1)
    await billing(telegram).run()

    assert cards.charged == []
    assert (await state(storage, user)).status == SubscriptionStatus.CANCELLED.value
    over = texts.subscription_term_over(LITE, stars=262).text
    assert [sent.text for sent in messenger.texts] == [over]


async def test_a_charge_started_before_the_switch_is_not_repeated(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    cards: FakeCards,
) -> None:
    """Списание за период начато до выключения, исход неизвестен. Новой
    попытки нет: если первое прошло, его доведёт сверка (З4)."""
    order = await storage.create_payment(
        user_id=user.id,
        tariff=LITE,
        method=PaymentMethod.CARD.value,
        amount=299,
        currency=RUB,
        docs_version="2026-08-31",
    )
    await subscribe(
        telegram, user, ends_in=-timedelta(minutes=1), charge_order_id=order.id
    )

    await billing(telegram).run()

    assert cards.charged == [] and cards.status_asked == []


@pytest.mark.parametrize("trial", [False, True], ids=["renewal", "after-trial"])
async def test_a_day_before_the_end_stars_are_offered(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    trial: bool,
) -> None:
    await subscribe(telegram, user, ends_in=timedelta(hours=12), trial=trial)

    await billing(telegram).run()
    await billing(telegram).run()

    (sent,) = messenger.texts
    assert sent.text.startswith("Завтра, ")
    assert "закончится тариф «Лайт»" in sent.text
    assert "262 ⭐" in sent.text
    no_charge_promise(messenger)
    assert sent.keyboard is not None
    button = sent.keyboard.rows[0][0]
    assert button.text == "⭐ Продлить за 262 ⭐"
    assert button.action == buy_action("lite")

    clock.advance(hours=13)
    await billing(telegram).run()

    assert cards.charged == []
    assert len(messenger.texts) == 1, "после предупреждения второго сообщения нет"
    assert (await state(storage, user)).status == SubscriptionStatus.CANCELLED.value


async def test_the_paid_term_keeps_working(
    telegram: Deps, user: User, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    await subscribe(telegram, user, ends_in=timedelta(hours=12))
    ends = (await storage.get_user_by_id(user.id)).tariff_expires_at  # type: ignore[union-attr]

    await billing(telegram).run()

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.tariff is LITE and found.tariff_expires_at == ends


async def test_without_stars_on_sale_the_notice_leads_to_the_tariffs(
    telegram: Deps, user: User, messenger: FakeMessenger
) -> None:
    no_stars = replace(
        telegram, settings=replace(telegram.settings, stars_enabled=False)
    )
    await subscribe(no_stars, user, ends_in=timedelta(hours=12))

    await billing(no_stars).run()

    (sent,) = messenger.texts
    assert "⭐" not in sent.text and "звёзд" not in sent.text
    assert sent.keyboard is not None
    assert sent.keyboard.rows[0][0].text == texts.MENU_TARIFFS


async def test_a_new_price_is_not_announced_for_a_term_that_will_not_renew(
    telegram: Deps, user: User, messenger: FakeMessenger
) -> None:
    """«Со следующего списания — по новой цене» тут неправда: списания нет."""
    await subscribe(telegram, user, ends_in=timedelta(days=5), amount=199)

    await billing(telegram).run()

    assert messenger.texts == []


async def test_renewing_by_stars_continues_from_the_end_of_the_term(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    stars: FakeStars,
) -> None:
    """Продлил звёздами до конца срока — оплаченные дни не пропадают, а
    звёздная подписка заменяет карточную."""
    from app.core.models import Chat, IncomingMessage
    from app.core.router import handle
    from app.core.scenarios import payments

    await subscribe(telegram, user, ends_in=timedelta(hours=12))
    ends = (await storage.get_user_by_id(user.id)).tariff_expires_at  # type: ignore[union-attr]
    chat = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")

    await handle(
        telegram,
        IncomingMessage(chat=chat, external_user_id="1", action=buy_action("lite")),
    )
    (invoice,) = stars.invoices
    await payments.confirm(telegram, invoice.order_id, charge_id="charge-1")

    found = await storage.get_user_by_id(user.id)
    assert found is not None and ends is not None
    assert found.tariff_expires_at == ends + timedelta(days=30)
    subscription = await state(storage, user)
    assert subscription.method == PaymentMethod.STARS.value


async def test_the_charge_reminder_itself_says_nothing_of_299(
    telegram: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Напоминание «спишем 299 ₽» молчит и само по себе — не только потому,
    что проход о конце срока успел раньше."""
    from app.core.scenarios import subscriptions

    subscription = await subscribe(
        telegram, user, ends_in=timedelta(hours=12), trial=True
    )

    await subscriptions.remind(telegram, subscription)

    assert messenger.texts == []


# --- З4: заказ картой, созданный до выключения и оплаченный позже ----------


async def old_card_order(deps: Deps, user: User, *, trial: bool) -> str:
    """Заказ со ссылкой ЮKassa, выданной до выключения карты."""
    order = await deps.storage.create_payment(
        user_id=user.id,
        tariff=LITE,
        method=PaymentMethod.CARD.value,
        amount=1 if trial else 299,
        currency=RUB,
        docs_version="2026-08-31",
        trial=trial,
    )
    await deps.storage.attach_external_id(order.id, f"yk-{order.id}")
    await deps.storage.remember_link(
        order.id, url="https://pay.example/old", receipt_email=None
    )
    return order.id


@pytest.mark.parametrize("trial", [False, True], ids=["lite", "trial"])
async def test_a_late_paid_card_order_is_settled_by_reconcile_once(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    trial: bool,
) -> None:
    from app.core.reconcile import Reconciler

    await old_card_order(telegram, user, trial=trial)
    clock.advance(minutes=20)
    reconciler = Reconciler(
        by_messenger={MessengerKind.TELEGRAM: telegram},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    )

    await reconciler.run()
    granted = (await storage.get_user_by_id(user.id)).tariff_expires_at  # type: ignore[union-attr]
    await reconciler.run()

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.tariff is LITE and found.tariff_expires_at == granted
    (done,) = [sent.text for sent in messenger.texts if sent.text.startswith("Готово")]
    assert "₽" not in done and "продлевается сам" not in done
    assert cards.created == [] and cards.charged == []


async def test_a_late_paid_card_order_is_settled_on_demand_once(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    from app.core.actions import Action
    from app.core.models import Chat, IncomingMessage
    from app.core.router import handle

    await old_card_order(telegram, user, trial=True)
    chat = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")

    def press(action: str) -> IncomingMessage:
        return IncomingMessage(chat=chat, external_user_id="1", action=action)

    await handle(telegram, press(Action.MENU_PROFILE))
    await handle(telegram, press(Action.MENU_PROFILE))

    started = [sent for sent in messenger.texts if sent.text.startswith("Готово")]
    assert [sent.text for sent in started] == [
        texts.trial_started_not_renewing(
            until=texts.format_date(
                (await storage.get_user_by_id(user.id)).tariff_expires_at.date()  # type: ignore[union-attr]
            )
        ).text
    ]
    assert cards.created == []


async def test_after_a_late_paid_trial_nothing_is_charged(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    cards: FakeCards,
    clock: FrozenClock,
) -> None:
    """Пробный период из старого заказа дорабатывает — и 299 ₽ после него
    не списываются."""
    from app.core.scenarios import payments

    order_id = await old_card_order(telegram, user, trial=True)
    await payments.confirm(telegram, order_id)

    for _ in range(4):
        clock.advance(days=1)
        await billing(telegram).run()

    assert cards.charged == []
