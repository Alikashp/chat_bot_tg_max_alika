"""Одна живая ссылка на одну покупку (сессия 14, шаг 1, О1 и О4).

В бою за 14–21.10 четыре человека заплатили по 1 ₽ лишний раз: оплата
подтверждалась сверкой через 10–14 минут, человек тем временем снова жал
«за 1 ₽» и получал новую рабочую ссылку. Теперь, пока у человека есть
неоплаченный заказ на ту же покупку моложе срока жизни ссылки, любая кнопка
покупки отдаёт ту же ссылку и второго заказа не заводит.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

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
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.receipts import FiscalSettings
from app.core.router import handle
from app.core.scenarios.deps import Deps
from app.ports.payments import ChargeStatus, PaymentStatus
from tests.fakes import FakeCards, FakeMessenger, FakeStars, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
FIRST = "https://pay.example/first"
SECOND = "https://pay.example/second"
FISCAL = FiscalSettings(
    vat_code=1, payment_subject="service", payment_mode="full_payment"
)


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def write(text: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", text=text)


@pytest.fixture
def cards() -> FakeCards:
    """Магазин с автоплатежами; оплаты пока нет — человек только взял ссылку."""
    found = FakeCards(recurring=True)
    found.paid = False
    found.confirmation_url = FIRST
    return found


@pytest.fixture
def shop(deps: Deps, cards: FakeCards) -> Deps:
    # Звёзды выключены, как в бою: способ один, и кнопка тарифа сразу ведёт
    # к заказу картой.
    return replace(
        deps,
        cards=cards,
        settings=replace(deps.settings, trial_enabled=True, stars_enabled=False),
    )


def links(messenger: FakeMessenger) -> list[str]:
    """Ссылки оплаты из всех экранов заказа — по порядку."""
    return [
        button.url
        for sent in messenger.texts
        if sent.keyboard is not None
        for row in sent.keyboard.rows
        for button in row
        if button.text == texts.BUTTON_PAY_OPEN and button.url
    ]


def orders(storage: InMemoryStorage, user: User) -> list[str]:
    return [order.id for order in storage.orders_of(user.id)]


# --- О1: повтор с любого входа — та же ссылка ---------------------------


@pytest.mark.parametrize(
    "second",
    [Action.TRIAL, Action.PAYWALL_TRIAL],
    ids=["tariffs", "paywall"],
)
async def test_a_repeated_trial_press_gives_the_same_link(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    second: str,
) -> None:
    await handle(shop, press(Action.TRIAL))
    cards.confirmation_url = SECOND
    clock.advance(minutes=5)

    await handle(shop, press(second))

    assert links(messenger) == [FIRST, FIRST]
    assert len(cards.created) == 1
    assert len(orders(storage, user)) == 1


async def test_a_double_press_makes_one_order(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Два нажатия разом: второе ждёт ссылку первого, а не заводит свою."""
    cards.slow = True

    await asyncio.gather(
        handle(shop, press(Action.TRIAL)), handle(shop, press(Action.TRIAL))
    )

    assert len(cards.created) == 1
    assert len(orders(storage, user)) == 1
    assert links(messenger) == [FIRST, FIRST]


@pytest.mark.parametrize(
    "again",
    [
        [buy_action("lite")],
        [method_action("card", "lite")],
        [Action.PAYWALL_TRIAL],
    ],
    ids=["tariff-button", "card-button", "paywall-without-trial"],
)
async def test_a_full_price_order_keeps_one_link(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    again: list[str],
) -> None:
    """О4: то же правило для покупки Лайта за полную цену."""
    no_trial = replace(shop, settings=replace(shop.settings, trial_enabled=False))
    await handle(no_trial, press(buy_action("lite")))
    cards.confirmation_url = SECOND
    clock.advance(minutes=5)

    for action in again:
        await handle(no_trial, press(action))

    assert links(messenger) == [FIRST, FIRST]
    assert len(orders(storage, user)) == 1


async def test_another_tariff_is_another_purchase(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    await handle(shop, press(buy_action("lite")))
    cards.confirmation_url = SECOND

    await handle(shop, press(buy_action("pro")))

    assert links(messenger) == [FIRST, SECOND]
    assert len(orders(storage, user)) == 2


async def test_the_trial_and_full_price_lite_are_different_purchases(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    await handle(shop, press(Action.TRIAL))
    cards.confirmation_url = SECOND

    await handle(shop, press(method_action("card", "lite")))

    assert links(messenger) == [FIRST, SECOND]


# --- Когда ссылка уже не живая -------------------------------------------


async def test_a_link_cancelled_by_yookassa_is_replaced(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """ЮKassa отменила платёж (человек не оплатил вовремя) — новая ссылка."""
    await handle(shop, press(Action.TRIAL))
    (first_order,) = orders(storage, user)
    cards.states["ext-1"] = ChargeStatus.REFUSED
    cards.confirmation_url = SECOND

    await handle(shop, press(Action.TRIAL))

    assert links(messenger) == [FIRST, SECOND]
    old = await storage.get_payment(first_order)
    assert old is not None and old.status == PaymentStatus.CANCELED.value


async def test_a_link_older_than_its_lifetime_is_replaced(
    shop: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
) -> None:
    await handle(shop, press(Action.TRIAL))
    cards.confirmation_url = SECOND
    clock.advance(minutes=shop.settings.payment_link_ttl_minutes + 1)

    await handle(shop, press(Action.TRIAL))

    assert links(messenger) == [FIRST, SECOND]


async def test_a_provider_that_does_not_answer_keeps_the_same_link(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Не узнали, жива ли ссылка, — не рискуем второй рабочей."""
    await handle(shop, press(Action.TRIAL))
    cards.status_error = ConnectionError("ЮKassa молчит")
    cards.confirmation_url = SECOND

    await handle(shop, press(Action.TRIAL))

    assert links(messenger) == [FIRST, FIRST]
    assert len(orders(storage, user)) == 1


async def test_an_order_whose_link_failed_does_not_block_the_next(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Ссылка не открылась — заказ закрыт, следующее нажатие заводит новый."""
    cards.error = ConnectionError("ЮKassa не ответила")
    await handle(shop, press(Action.TRIAL))
    cards.error = None

    await handle(shop, press(Action.TRIAL))

    assert links(messenger) == [FIRST]
    first, second = storage.orders_of(user.id)
    assert first.status == PaymentStatus.CANCELED.value
    assert second.status == PaymentStatus.PENDING.value


# --- Почта, изменённая после выдачи ссылки ------------------------------


async def test_a_changed_email_keeps_the_link_and_says_where_its_receipt_goes(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Неоплаченный платёж ЮKassa не отменить: чек выданной ссылки остаётся
    на прежней почте, новая — для следующих чеков. Второй ссылки нет."""
    fiscal = replace(shop, settings=replace(shop.settings, fiscal=FISCAL))
    await handle(fiscal, press(Action.TRIAL))
    await handle(fiscal, write("first@mail.ru"))
    cards.confirmation_url = SECOND

    await handle(fiscal, press(email_action(TRIAL_TARGET)))
    await handle(fiscal, write("second@mail.ru"))

    assert links(messenger) == [FIRST, FIRST]
    assert len(cards.created) == 1
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.email == "second@mail.ru"
    note = texts.email_kept_for_next(new="second@mail.ru", issued="first@mail.ru")
    assert note.text in [sent.text for sent in messenger.texts]
    # Экран заказа называет ту почту, на которую правда уйдёт чек.
    assert "first@mail.ru" in messenger.texts[-1].text


async def test_the_same_email_again_adds_no_note(
    shop: Deps,
    user: User,
    messenger: FakeMessenger,
) -> None:
    fiscal = replace(shop, settings=replace(shop.settings, fiscal=FISCAL))
    await handle(fiscal, press(Action.TRIAL))
    await handle(fiscal, write("first@mail.ru"))

    await handle(fiscal, press(email_action(TRIAL_TARGET)))
    await handle(fiscal, write("first@mail.ru"))

    note = texts.email_kept_for_next(new="first@mail.ru", issued="first@mail.ru")
    assert note.text not in [sent.text for sent in messenger.texts]
    assert links(messenger) == [FIRST, FIRST]


# --- Звёзды -----------------------------------------------------------------


async def test_a_repeated_stars_press_gives_the_same_invoice(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    stars: FakeStars,
) -> None:
    by_stars = replace(shop, settings=replace(shop.settings, stars_enabled=True))

    await handle(by_stars, press(method_action("stars", "lite")))
    await handle(by_stars, press(method_action("stars", "lite")))

    assert len(stars.invoices) == 1
    assert len(orders(storage, user)) == 1
    (first,) = set(links(messenger))
    assert first.startswith("https://t.me/invoice/")


def test_the_link_lifetime_is_a_setting() -> None:
    from app.core.settings import CoreSettings

    assert CoreSettings(bot_username="bot").payment_link_ttl_minutes == 60


async def test_a_link_still_being_made_is_waited_for_not_doubled(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Соседнее нажатие полминуты назад завело заказ и ещё ждёт ЮKassa:
    второй ссылки нет — честный ответ, что она придёт."""
    from app.core.scenarios import payments

    monkeypatch.setattr(payments, "_LINK_WAIT_SECONDS", 0.3)
    await storage.open_or_create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=1,
        currency="RUB",
        docs_version="2026-10-12",
        trial=True,
        linked_after=clock.now,
        making_after=clock.now,
    )
    clock.advance(seconds=30)

    await handle(shop, press(Action.TRIAL))

    assert cards.created == []
    assert messenger.last_text.text == texts.payment_link_coming().text


async def test_a_crashed_press_does_not_block_after_a_minute(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    await storage.open_or_create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=1,
        currency="RUB",
        docs_version="2026-10-12",
        trial=True,
        linked_after=clock.now,
        making_after=clock.now,
    )
    clock.advance(seconds=61)

    await handle(shop, press(Action.TRIAL))

    assert links(messenger) == [FIRST]
