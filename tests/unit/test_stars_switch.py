"""Оплата звёздами выключается настройкой (сессия 11, шаг 2, П4–П5).

По умолчанию выключена. Тогда новых звёздных заказов нет: ни выбора
способа, ни цены в звёздах, ни счёта, а pre-checkout по новому звёздному
заказу отклоняется. Карта показывается сразу. Код не удалён: действующие
звёздные подписки продлеваются и засчитываются как раньше, сверка журнала
звёзд работает.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import buy_action, method_action
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.reconcile import Reconciler
from app.core.router import handle
from app.core.scenarios import payments, tariffs
from app.core.scenarios.deps import Deps, Session
from app.ports.payments import PaymentMethod, StarPayment
from tests.fakes import FakeMessenger, FakeStars, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
PRO = TariffId.PRO


def off(deps: Deps) -> Deps:
    return replace(deps, settings=replace(deps.settings, stars_enabled=False))


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def mentions_stars(messenger: FakeMessenger) -> bool:
    """Звёзды на экране: слово, цена в звёздах или кнопка оплаты звёздами.

    Значок ⭐ у «⭐ Тарифы» и «⭐ Открыть тарифы» — не звёзды Telegram.
    """
    shown = [sent.text for sent in messenger.texts] + [
        button.text
        for sent in messenger.texts
        if sent.keyboard is not None
        for row in sent.keyboard.rows
        for button in row
    ]
    return any(
        re.search(r"звёзд|Звёзд|\d ⭐", each) is not None
        or each == texts.BUTTON_PAY_STARS
        for each in shown
    )


async def test_with_stars_off_the_card_comes_straight_away(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    stars: FakeStars,
    messenger: FakeMessenger,
) -> None:
    """П4: нет вопроса о способе, нет цены в звёздах — сразу заказ картой."""
    await handle(off(deps), press(buy_action(PRO.value)))

    assert texts.BUTTON_PAY_STARS not in [
        b.text
        for sent in messenger.texts
        if sent.keyboard is not None
        for row in sent.keyboard.rows
        for b in row
    ]
    assert not mentions_stars(messenger)
    (order,) = storage._payments.values()
    assert order.method == PaymentMethod.CARD.value
    assert stars.invoices == []


async def test_with_stars_on_both_methods_are_offered(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Настройка включена — всё как раньше: выбор способа с ценой в звёздах."""
    on = replace(deps, settings=replace(deps.settings, stars_enabled=True))

    await handle(on, press(buy_action(PRO.value)))

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert texts.BUTTON_PAY_STARS in [b.text for row in keyboard.rows for b in row]


async def test_an_old_stars_button_creates_no_star_order(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    stars: FakeStars,
    messenger: FakeMessenger,
) -> None:
    """П4: кнопка «⭐ Звёздами» из старого сообщения ведёт к карте."""
    await handle(off(deps), press(method_action(PaymentMethod.STARS.value, PRO.value)))

    assert stars.invoices == []
    assert all(
        order.method != PaymentMethod.STARS.value
        for order in storage._payments.values()
    )
    assert not mentions_stars(messenger)


async def test_with_stars_off_and_no_card_nothing_mentions_stars(
    deps: Deps, user: User, stars: FakeStars, messenger: FakeMessenger
) -> None:
    """Магазина карт нет, звёзды выключены — оплаты нет, звёзд не обещаем."""
    await handle(replace(off(deps), cards=None), press(buy_action(PRO.value)))

    assert stars.invoices == []
    assert not mentions_stars(messenger)


async def test_the_tariffs_screen_does_not_mention_stars(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await tariffs.show(off(deps), session)

    assert not mentions_stars(messenger)


async def test_a_new_star_order_is_refused_at_pre_checkout(
    deps: Deps, session: Session, stars: FakeStars
) -> None:
    """П4: счёт выставили, пока звёзды были включены, — платить по нему нельзя."""
    on = replace(deps, settings=replace(deps.settings, stars_enabled=True))
    await payments.start_stars(on, session, PRO)
    order_id = stars.invoices[0].order_id

    await payments.approve(off(deps), "req-1", order_id, user=session.user)

    assert stars.approvals == [("req-1", False)]


async def test_a_live_star_subscription_renews_once_with_stars_off(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    """П5: действующая подписка продлевается, засчитывается ровно один раз."""
    on = replace(deps, settings=replace(deps.settings, stars_enabled=True))
    await payments.start_stars(on, session, PRO)
    first = stars.invoices[0].order_id
    await payments.confirm(on, first, charge_id="charge-1")
    found = await storage.get_user_by_id(session.user.id)
    assert found is not None and found.tariff_expires_at is not None
    expires = found.tariff_expires_at
    clock.advance(days=30)
    disabled = off(deps)

    await payments.approve(disabled, "req-renew", first, user=session.user)
    granted = await payments.confirm(
        disabled, first, charge_id="charge-2", renewal=True
    )
    again = await payments.confirm(disabled, first, charge_id="charge-2", renewal=True)
    stars.transactions = [
        StarPayment("charge-1", session.user.external_id, first, clock.now),
        StarPayment("charge-2", session.user.external_id, first, clock.now),
    ]
    await Reconciler(
        by_messenger={MessengerKind.TELEGRAM: disabled},
        after=timedelta(minutes=10),
        window=timedelta(hours=72),
    ).run()

    assert stars.approvals == [("req-renew", True)]
    assert granted is not None and again is None
    renewed = await storage.get_user_by_id(session.user.id)
    assert renewed is not None
    assert renewed.tariff_expires_at == expires + timedelta(days=30)


async def test_a_lost_renewal_is_reconciled_with_stars_off(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    """П5: сверка журнала звёзд работает и при выключенных звёздах."""
    on = replace(deps, settings=replace(deps.settings, stars_enabled=True))
    await payments.start_stars(on, session, PRO)
    first = stars.invoices[0].order_id
    await payments.confirm(on, first, charge_id="charge-1")
    found = await storage.get_user_by_id(session.user.id)
    assert found is not None and found.tariff_expires_at is not None
    expires = found.tariff_expires_at
    clock.advance(days=30)
    stars.transactions = [
        StarPayment("charge-1", session.user.external_id, first, clock.now),
        StarPayment("charge-2", session.user.external_id, first, clock.now),
    ]

    for _ in range(2):
        await Reconciler(
            by_messenger={MessengerKind.TELEGRAM: off(deps)},
            after=timedelta(minutes=10),
            window=timedelta(hours=72),
        ).run()

    renewed = await storage.get_user_by_id(session.user.id)
    assert renewed is not None
    assert renewed.tariff_expires_at == expires + timedelta(days=30)
