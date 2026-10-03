"""Сверка зависших заказов (фаза 11, П5).

Уведомление об оплате могло не дойти, упасть на перечитывании платежа или
потеряться с перезапуском между ответом 200 и обработкой. ЮKassa в двух
последних случаях повторять не станет: 200 мы уже ответили. Человек заплатил —
и остался без тарифа. Сверка — периодический проход по заказам pending с
известным платежом: перечитать его ключом магазина человека и довести
обычным подтверждением.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.limits import current_day
from app.core.models import MessengerKind, Subscription, TariffId, User
from app.core.reconcile import Reconciler
from app.core.scenarios.deps import Deps
from app.ports.payments import PaymentStatus
from tests.fakes import FakeCards, FakeLogger, FakeMessenger, FrozenClock

AFTER = timedelta(minutes=10)
WINDOW = timedelta(days=3)


async def _order(
    storage: InMemoryStorage, user: User, external_id: str = "ext-1"
) -> str:
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    assert await storage.attach_external_id(order.id, external_id)
    return order.id


def _reconciler(by_messenger: dict[MessengerKind, Deps]) -> Reconciler:
    return Reconciler(by_messenger=by_messenger, after=AFTER, window=WINDOW, batch=50)


async def test_a_paid_order_nobody_confirmed_is_granted(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    cards: FakeCards,
) -> None:
    """П5: уведомление не пришло — сверка перечитывает платёж и выдаёт тариф."""
    order_id = await _order(storage, user)
    clock.advance(minutes=11)

    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    assert cards.asked == ["ext-1"]
    order = await storage.get_payment(order_id)
    assert order is not None and order.status == PaymentStatus.PAID.value
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.tariff is TariffId.PRO
    assert fresh.tariff_expires_at is not None
    day = current_day(fresh.tariff_expires_at, deps.settings.timezone)
    expected = texts.payment_done(
        TariffId.PRO, until=texts.format_date(day), renewing=False
    )
    assert messenger.last_text.text == expected.text


async def test_a_fresh_order_is_left_to_the_notice(
    deps: Deps, user: User, storage: InMemoryStorage, cards: FakeCards
) -> None:
    """Заказ моложе порога не трогаем: уведомление обычно приходит за секунды."""
    await _order(storage, user)

    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    assert cards.asked == []


async def test_an_unpaid_order_stays_pending(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    cards: FakeCards,
) -> None:
    order_id = await _order(storage, user)
    cards.paid = False
    clock.advance(minutes=11)

    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    order = await storage.get_payment(order_id)
    assert order is not None and order.status == PaymentStatus.PENDING.value


async def test_an_order_older_than_the_window_is_not_asked_about(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    cards: FakeCards,
) -> None:
    """Совсем старые заказы не перечитываются вечно."""
    await _order(storage, user)
    clock.advance(days=4)

    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    assert cards.asked == []


async def test_a_failing_order_does_not_stop_the_rest(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    cards: FakeCards,
    logger: FakeLogger,
) -> None:
    """Провайдер упал на одном заказе — остальные доводятся в этот же проход."""
    first = await _order(storage, user, "ext-broken")
    second = await _order(storage, user, "ext-ok")
    clock.advance(minutes=11)
    real = cards.is_paid

    async def flaky(external_id: str, *, expected_rub: int) -> bool:
        if external_id == "ext-broken":
            raise RuntimeError("ЮKassa не ответила")
        return await real(external_id, expected_rub=expected_rub)

    cards.is_paid = flaky  # type: ignore[method-assign]
    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    assert (await storage.get_payment(first)).status == PaymentStatus.PENDING.value  # type: ignore[union-attr]
    assert (await storage.get_payment(second)).status == PaymentStatus.PAID.value  # type: ignore[union-attr]
    assert "reconcile_failed" in logger.names()


async def test_the_order_is_read_in_the_shop_of_its_messenger(
    deps: Deps, storage: InMemoryStorage, clock: FrozenClock, cards: FakeCards
) -> None:
    """П6 и П5: заказ человека из MAX перечитывается ключом магазина MAX."""
    from app.core import support

    person = await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="max-1",
        referral_code="maxcode1",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    await _order(storage, person, "ext-max")
    clock.advance(minutes=11)
    max_cards = FakeCards()
    in_max = replace(deps, cards=max_cards, messenger=FakeMessenger())

    await _reconciler({MessengerKind.TELEGRAM: deps, MessengerKind.MAX: in_max}).run()

    assert (cards.asked, max_cards.asked) == ([], ["ext-max"])


async def test_a_notice_failing_on_the_reread_is_finished_by_reconciliation(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    cards: FakeCards,
) -> None:
    """П5 целиком: уведомление упало на перечитывании, сверка довела заказ раз."""
    order_id = await _order(storage, user)
    clock.advance(minutes=11)

    await _reconciler({MessengerKind.TELEGRAM: deps}).run()
    await _reconciler({MessengerKind.TELEGRAM: deps}).run()

    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    assert fresh.tariff_expires_at == clock() + timedelta(days=30)
    order = await storage.get_payment(order_id)
    assert order is not None and order.status == PaymentStatus.PAID.value


async def test_a_renewal_order_is_announced_as_a_renewal(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Списание за период, доведённое сверкой, — это продление, а не покупка.

    Человек ничего не покупал: экран «Оплата прошла» его бы только запутал.
    """
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    order_id = await _order(storage, user)
    await storage.save_subscription(
        Subscription(
            user_id=user.id,
            tariff=TariffId.PRO,
            method="card",
            status="active",
            amount=599,
            currency="RUB",
            next_charge_at=clock.now,
            created_at=clock.now,
            payment_method_id="card-1",
        )
    )
    assert await storage.hold_charge_order(user.id, order_id)
    clock.advance(minutes=11)

    await _reconciler({MessengerKind.TELEGRAM: recurring}).run()

    assert "Продлили тариф" in messenger.last_text.text
