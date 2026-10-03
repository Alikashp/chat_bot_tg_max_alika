"""Пробный период тарифа Лайт: 3 дня за 1 ₽, затем 299 ₽ каждые 30 дней.

Фаза 11, часть 3, критерии ПП1–ПП8. Пробный период — та же подписка картой,
только первый платёж маленький и короткий, а перед первым полным списанием
человека обязательно предупреждают.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action
from app.core.billing import Billing
from app.core.limits import LimitKind, monthly_norm
from app.core.models import (
    Chat,
    IncomingMessage,
    MessengerKind,
    Payment,
    TariffId,
    User,
)
from app.core.receipts import FiscalSettings
from app.core.router import handle
from app.core.scenarios import payments, spending, subscriptions, tariffs
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import RUB, TRIAL, tariff_of
from app.ports.payments import SubscriptionStatus
from tests.fakes import FakeCards, FakeLogger, FakeMessenger, FakeStars, FrozenClock

LITE = TariffId.LITE
#: Пробный период взят 28 августа в 15:00 по Москве — кончается 31-го.
FIRST_CHARGE = "31 августа"


@pytest.fixture
def cards() -> FakeCards:
    """Магазин с автоплатежами: без них пробный период не предлагается."""
    return FakeCards(recurring=True)


@pytest.fixture
def trial(deps: Deps, cards: FakeCards) -> Deps:
    """Пробный период включён настройкой, карта умеет повторные списания."""
    return replace(
        deps, cards=cards, settings=replace(deps.settings, trial_enabled=True)
    )


async def _fresh(deps: Deps, user: User) -> Session:
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None
    return Session(
        user=found,
        chat=Chat(messenger=found.messenger, chat_id=found.external_id),
        day=deps.today(),
        now=deps.now(),
    )


async def _take(trial: Deps, session: Session, cards: FakeCards) -> Payment:
    """Человек оформил пробный период и заплатил 1 ₽."""
    await payments.start_trial(trial, session)
    order = await payments.confirm(trial, cards.created[-1][0])
    assert order is not None
    return order


def _offered(messenger: FakeMessenger) -> bool:
    return any(sent.text == texts.trial_offer().text for sent in messenger.texts)


# --- ПП1: кому предлагается -----------------------------------------------


async def test_someone_who_never_paid_sees_the_offer_under_the_tariffs(
    trial: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await tariffs.show(trial, session)

    assert (
        messenger.texts[0].text == texts.tariffs_screen(with_presentations=False).text
    ), "карточки тарифов остаются дословными"
    assert messenger.last_text.text == texts.trial_offer().text
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert [b.action for row in keyboard.rows for b in row] == [Action.TRIAL]


async def test_someone_who_paid_once_sees_only_the_tariffs(
    trial: Deps, session: Session, messenger: FakeMessenger, stars: FakeStars
) -> None:
    """Звёзды — тоже оплата: пробный период только для тех, кто не платил."""
    await payments.start_stars(trial, session, TariffId.PRO)
    await payments.confirm(trial, stars.invoices[0].order_id, charge_id="charge-1")
    messenger.texts.clear()

    await tariffs.show(trial, await _fresh(trial, session.user))

    assert not _offered(messenger)
    assert len(messenger.texts) == 1


async def test_someone_who_took_the_trial_sees_only_the_tariffs(
    trial: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    await _take(trial, session, cards)
    messenger.texts.clear()

    await tariffs.show(trial, await _fresh(trial, session.user))

    assert not _offered(messenger)


async def test_an_old_offer_button_answers_with_the_tariffs(
    trial: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    """Кнопка из старого сообщения после оплаты — обычные тарифы, а не счёт."""
    await _take(trial, session, cards)
    created = len(cards.created)

    await payments.start_trial(trial, await _fresh(trial, session.user))

    assert len(cards.created) == created
    assert (
        messenger.last_text.text == texts.tariffs_screen(with_presentations=False).text
    )


# --- ПП2: условия до денег -------------------------------------------------


async def test_the_order_screen_names_every_condition(
    trial: Deps,
    session: Session,
    messenger: FakeMessenger,
    cards: FakeCards,
    storage: InMemoryStorage,
) -> None:
    await payments.start_trial(trial, session)

    screen = texts.trial_order(
        first_charge=FIRST_CHARGE,
        statement=trial.settings.bank_statement_name,
    )
    assert messenger.last_text.text == screen.text
    for promise in ("3 дня за 1 ₽", "299 ₽ каждые 30 дней", FIRST_CHARGE):
        assert promise in screen.text
    assert "Отключить" in screen.text and texts.CONSENT in screen.text
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    buttons = [b for row in keyboard.rows for b in row]
    assert [b.text for b in buttons] == [
        texts.BUTTON_PAY_OPEN,
        texts.BUTTON_OFFER,
        texts.BUTTON_PRIVACY,
    ]
    assert buttons[1].url == trial.settings.offer_url
    assert buttons[2].url == trial.settings.privacy_url

    order_id, amount = cards.created[0]
    assert amount == TRIAL.price_rub
    assert cards.saved_requested is True, "без сохранённой карты 299 ₽ не списать"
    order = await storage.get_payment(order_id)
    assert order is not None
    assert (order.trial, order.amount, order.tariff) == (True, 1, LITE)
    assert order.docs_version == trial.settings.docs_version


# --- Выдача ---------------------------------------------------------------


async def test_the_trial_gives_lite_for_three_days(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
) -> None:
    order = await _take(trial, session, cards)
    await payments.announce(trial, session, order)

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff is LITE
    assert user.tariff_expires_at == trial.now() + timedelta(days=3)
    assert user.trial_order_id == order.id
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.amount == tariff_of(LITE).price_rub
    assert subscription.next_charge_at == user.tariff_expires_at
    assert subscription.remind_before_charge is True
    assert subscription.payment_method_id == "card-1"
    assert (
        messenger.last_text.text
        == texts.trial_started(until=FIRST_CHARGE, amount=299).text
    )


async def test_the_trial_days_have_the_lite_norms(
    trial: Deps, session: Session, cards: FakeCards
) -> None:
    await _take(trial, session, cards)

    during = await _fresh(trial, session.user)
    for kind in (LimitKind.IMAGES, LimitKind.DOCUMENTS, LimitKind.PRESENTATIONS):
        left = await spending.current_allowance(trial, during, kind)
        assert left.monthly_limit == monthly_norm(tariff_of(LITE), kind)


# --- ПП3: ровно один раз ---------------------------------------------------


async def test_a_repeated_notice_gives_nothing_more(
    trial: Deps, session: Session, cards: FakeCards, storage: InMemoryStorage
) -> None:
    order = await _take(trial, session, cards)

    again = await payments.confirm(trial, order.id)

    assert again is None
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff_expires_at == trial.now() + timedelta(days=3)


async def test_a_second_paid_ruble_gives_no_second_trial(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    storage: InMemoryStorage,
    logger: FakeLogger,
    clock: FrozenClock,
) -> None:
    """Две ссылки взяты до оплаты, оплачены обе — пробный период один."""
    await payments.start_trial(trial, session)
    await payments.start_trial(trial, session)
    first, second = (order_id for order_id, _ in cards.created)
    assert await payments.confirm(trial, first) is not None
    clock.advance(hours=1)

    assert await payments.confirm(trial, second) is None

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.trial_order_id == first
    assert user.tariff_expires_at == trial.now() - timedelta(hours=1) + timedelta(
        days=3
    )
    unused = [e for e in logger.events if e.event == "trial_payment_unused"]
    assert [e.level for e in unused] == ["error"]
    paid = await storage.get_payment(second)
    assert paid is not None and paid.status == "paid", "деньги взяты — заказ оплачен"


async def test_two_orders_confirmed_at_once_give_one_trial(
    trial: Deps, session: Session, cards: FakeCards, storage: InMemoryStorage
) -> None:
    await payments.start_trial(trial, session)
    await payments.start_trial(trial, session)
    first, second = (order_id for order_id, _ in cards.created)

    granted = await asyncio.gather(
        payments.confirm(trial, first), payments.confirm(trial, second)
    )

    assert sum(order is not None for order in granted) == 1
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.tariff_expires_at == trial.now() + timedelta(days=3)


# --- ПП4: напоминание и первое списание -----------------------------------


async def _week(
    deps: Deps, messenger: FakeMessenger, clock: FrozenClock, cards: FakeCards
) -> list[tuple[str, str]]:
    """Проход биллинга каждый час неделю: списания и сообщения по Москве."""
    billing = Billing(by_messenger={MessengerKind.TELEGRAM: deps})
    calendar: list[tuple[str, str]] = []
    for _ in range(7 * 24):
        charges, said = len(cards.keys), len(messenger.texts_said())
        await billing.run()
        moment = clock.now.astimezone(_zone(deps)).strftime("%d.%m %H:%M")
        calendar += [(moment, "списание")] * (len(cards.keys) - charges)
        calendar += [(moment, text) for text in messenger.texts_said()[said:]]
        clock.advance(hours=1)
    return calendar


def _zone(deps: Deps) -> ZoneInfo:
    return ZoneInfo(deps.settings.timezone)


async def test_the_first_full_charge_comes_after_a_reminder_and_once(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    clock: FrozenClock,
    storage: InMemoryStorage,
) -> None:
    await _take(trial, session, cards)
    messenger.texts.clear()

    calendar = await _week(trial, messenger, clock, cards)

    reminder = texts.subscription_reminder(
        LITE, amount=299, currency=RUB, on=FIRST_CHARGE
    ).text
    assert calendar[:2] == [("30.08 15:00", reminder), ("31.08 15:00", "списание")]
    assert [entry for entry in calendar if entry[1] == "списание"] == [
        ("31.08 15:00", "списание")
    ]
    assert cards.charged[0][1] == 299
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.remind_before_charge is False, "дальше — обычные продления"


async def test_after_the_first_charge_the_norm_starts_over(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    await _take(trial, session, cards)
    during = await _fresh(trial, session.user)
    for _ in range(5):
        await spending.charge(trial, during, LimitKind.IMAGES)

    await _week(trial, messenger, clock, cards)

    after = await _fresh(trial, session.user)
    left = await spending.current_allowance(trial, after, LimitKind.IMAGES)
    assert left.monthly_used == 0
    assert left.monthly_limit == monthly_norm(tariff_of(LITE), LimitKind.IMAGES)
    assert after.user.tariff_expires_at is not None
    assert after.user.tariff_expires_at - after.now > timedelta(days=25)


async def test_without_a_delivered_reminder_there_is_no_charge(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    await _take(trial, session, cards)
    messenger.fail_send = RuntimeError("мессенджер лёг")
    billing = Billing(by_messenger={MessengerKind.TELEGRAM: trial})

    for _ in range(4 * 24):
        await billing.run()
        clock.advance(hours=1)

    assert cards.charged == []


# --- ПП5: отмена во время пробных дней ------------------------------------


async def test_cancelling_during_the_trial_takes_no_money(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    await _take(trial, session, cards)
    await subscriptions.cancel(trial, await _fresh(trial, session.user))

    still = await _fresh(trial, session.user)
    assert still.tariff.id is LITE, "пробные дни дорабатывают до конца"

    await _week(trial, messenger, clock, cards)

    assert cards.charged == []
    later = await _fresh(trial, session.user)
    assert later.tariff.id is TariffId.FREE
    left = await spending.current_allowance(trial, later, LimitKind.IMAGES)
    assert left.monthly_limit == monthly_norm(
        tariff_of(TariffId.FREE), LimitKind.IMAGES
    )


# --- ПП6: отказ банка при первом списании ---------------------------------


async def test_a_refused_first_charge_follows_the_usual_rules(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Три попытки с суточным шагом, о каждой — сообщение, затем конец."""
    await _take(trial, session, cards)
    cards.charge_succeeds = False
    messenger.texts.clear()

    calendar = await _week(trial, messenger, clock, cards)

    failed = texts.subscription_charge_failed
    assert calendar == [
        (
            "30.08 15:00",
            texts.subscription_reminder(
                LITE, amount=299, currency=RUB, on=FIRST_CHARGE
            ).text,
        ),
        ("31.08 15:00", "списание"),
        (
            "31.08 15:00",
            failed(LITE, amount=299, currency=RUB, next_try="1 сентября").text,
        ),
        ("01.09 15:00", "списание"),
        (
            "01.09 15:00",
            failed(LITE, amount=299, currency=RUB, next_try="2 сентября").text,
        ),
        ("02.09 15:00", "списание"),
        ("02.09 15:00", texts.subscription_ended(LITE).text),
    ]


# --- ПП7: где пробного периода нет ----------------------------------------


@pytest.mark.parametrize(
    "where",
    ["setting_off", "stars_only", "no_recurring"],
)
async def test_the_trial_is_never_offered_or_mentioned_where_it_is_off(
    trial: Deps,
    session: Session,
    messenger: FakeMessenger,
    cards: FakeCards,
    where: str,
) -> None:
    off = {
        "setting_off": replace(
            trial, settings=replace(trial.settings, trial_enabled=False)
        ),
        "stars_only": replace(trial, cards=None),
        "no_recurring": replace(trial, cards=FakeCards(recurring=False)),
    }[where]

    await tariffs.show(off, session)
    await payments.start_trial(off, session)

    assert not _offered(messenger)
    said = " ".join(messenger.texts_said()).lower()
    assert "1 ₽" not in said and "пробн" not in said
    assert cards.created == []


async def test_the_offer_button_routes_to_the_trial(
    trial: Deps, session: Session, cards: FakeCards
) -> None:
    pressed = IncomingMessage(
        chat=session.chat,
        external_user_id=session.user.external_id,
        action=Action.TRIAL,
    )

    await handle(trial, pressed)

    assert [amount for _, amount in cards.created] == [TRIAL.price_rub]


# --- ПП8: чек на 1 ₽ -------------------------------------------------------


async def test_the_receipt_for_the_ruble_goes_with_the_payment(
    trial: Deps, session: Session, cards: FakeCards, storage: InMemoryStorage
) -> None:
    fiscal = replace(
        trial,
        settings=replace(
            trial.settings,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )
    await storage.set_email(session.user.id, "alika@mail.ru")

    await payments.start_trial(fiscal, await _fresh(fiscal, session.user))

    receipt = cards.receipts[-1]
    assert receipt is not None
    assert (receipt.total_rub, receipt.email) == (1, "alika@mail.ru")


async def test_without_an_address_the_trial_asks_for_one_and_comes_back(
    trial: Deps, session: Session, cards: FakeCards, messenger: FakeMessenger
) -> None:
    fiscal = replace(
        trial,
        settings=replace(
            trial.settings,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )

    await payments.start_trial(fiscal, session)
    assert messenger.last_text.text == texts.email_ask().text
    await payments.remember_email(
        fiscal, await _fresh(fiscal, session.user), "alika@mail.ru"
    )

    assert [amount for _, amount in cards.created] == [1]
    receipt = cards.receipts[-1]
    assert receipt is not None and receipt.total_rub == 1


async def test_another_address_returns_to_the_trial_not_to_a_tariff(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    messenger: FakeMessenger,
    storage: InMemoryStorage,
) -> None:
    """«Другая почта» на экране пробного заказа ведёт обратно к пробному."""
    fiscal = replace(
        trial,
        settings=replace(
            trial.settings,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )
    await storage.set_email(session.user.id, "alika@mail.ru")
    await payments.start_trial(fiscal, await _fresh(fiscal, session.user))
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    change = [b for row in keyboard.rows for b in row][-1]
    assert change.text == texts.BUTTON_EMAIL_CHANGE and change.action is not None

    await handle(
        fiscal,
        IncomingMessage(
            chat=session.chat,
            external_user_id=session.user.external_id,
            action=change.action,
        ),
    )
    await handle(
        fiscal,
        IncomingMessage(
            chat=session.chat,
            external_user_id=session.user.external_id,
            text="new@mail.ru",
        ),
    )

    assert [amount for _, amount in cards.created] == [1, 1]
    receipt = cards.receipts[-1]
    assert receipt is not None and receipt.email == "new@mail.ru"


# --- Возврат --------------------------------------------------------------


async def test_a_refunded_trial_ends_at_once_without_a_charge(
    trial: Deps, session: Session, cards: FakeCards, storage: InMemoryStorage
) -> None:
    order = await _take(trial, session, cards)
    assert await storage.mark_refunded(order.id)

    await payments.refunded(trial, order)

    later = await _fresh(trial, session.user)
    assert later.tariff.id is TariffId.FREE
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.CANCELLED.value


async def test_refunding_the_ruble_later_keeps_the_paid_month(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Вернули 1 ₽ уже после списания 299 ₽ — пробные дни давно прошли.

    Отбирать нечего: оплаченный месяц оплачен другим заказом. Срок возврата
    у пробного заказа — его три дня, а не тридцать.
    """
    order = await _take(trial, session, cards)
    await _week(trial, messenger, clock, cards)
    paid = await _fresh(trial, session.user)
    assert paid.tariff.id is LITE
    assert await storage.mark_refunded(order.id)

    await payments.refunded(trial, order)

    later = await _fresh(trial, session.user)
    assert later.tariff.id is LITE
    assert later.user.tariff_expires_at == paid.user.tariff_expires_at


async def test_refunding_an_unused_ruble_keeps_the_trial(
    trial: Deps,
    session: Session,
    cards: FakeCards,
    storage: InMemoryStorage,
    clock: FrozenClock,
) -> None:
    """Лишний 1 ₽ вернули — пробный период, оплаченный первым, остаётся."""
    await payments.start_trial(trial, session)
    await payments.start_trial(trial, session)
    first, second = (order_id for order_id, _ in cards.created)
    await payments.confirm(trial, first)
    clock.advance(hours=1)
    await payments.confirm(trial, second)
    unused = await storage.get_payment(second)
    assert unused is not None and await storage.mark_refunded(second)

    await payments.refunded(trial, unused)

    later = await _fresh(trial, session.user)
    assert later.tariff.id is LITE
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.ACTIVE.value
