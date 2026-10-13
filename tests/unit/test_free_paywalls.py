"""Пейволлы бесплатного тарифа предлагают Лайт (сессия 11, шаг 1, П1–П3).

Бесплатному, у которого кончился ресурс, — не общий список тарифов, а одно
предложение: Лайт. Кому сейчас положен пробный период — за 1 ₽ на 3 дня,
остальным — за цену Лайта. Кнопка ведёт прямо к экрану условий или заказа,
минуя список тарифов. Платящим — прежние пейволлы.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action, buy_action
from app.core.limits import LimitKind
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.router import handle
from app.core.scenarios import payments, paywall
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import TRIAL, tariff_of
from tests.fakes import FakeCards, FakeMessenger, FakePresentations, FakeStars

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")

#: Ожидаемые тексты — дословно. Презентации — образец заказчика.
WITH_TRIAL = {
    LimitKind.PRESENTATIONS: (
        "Это была бесплатная презентация ✅\n"
        "Нужна ещё? Открой всё на 3 дня за 1 ₽:\n"
        "10 презентаций, доклады и 100 сообщений в день."
    ),
    LimitKind.IMAGES: (
        "Это были бесплатные картинки на этот месяц ✅\n"
        "Нужно ещё? Открой всё на 3 дня за 1 ₽:\n"
        "20 картинок, доклады и 100 сообщений в день."
    ),
    LimitKind.DOCUMENTS: (
        "Это были бесплатные доклады ✅\n"
        "Нужно ещё? Открой всё на 3 дня за 1 ₽:\n"
        "15 докладов, картинки и 100 сообщений в день."
    ),
    LimitKind.MESSAGES: (
        "Это были бесплатные сообщения на сегодня ✅\n"
        "Нужно ещё? Открой всё на 3 дня за 1 ₽:\n"
        "100 сообщений в день, картинки и доклады."
    ),
}
WITHOUT_TRIAL = {
    kind: text.replace("на 3 дня за 1 ₽:", "за 299 ₽ в месяц:")
    for kind, text in WITH_TRIAL.items()
}
INVITES = {
    LimitKind.PRESENTATIONS: "🎁 Позвать друга → +1 презентация",
    LimitKind.IMAGES: "🎁 Позвать друга → +2 картинки сразу",
    LimitKind.DOCUMENTS: None,
    LimitKind.MESSAGES: "🎁 Позвать друга → +20 сообщений сразу",
}


@pytest.fixture
def trial_on(deps: Deps) -> Deps:
    """Пробный период включён, магазин умеет автоплатежи, раздел презентаций есть."""
    return replace(
        deps,
        cards=FakeCards(recurring=True),
        presentations=FakePresentations(),
        settings=replace(deps.settings, trial_enabled=True),
    )


@pytest.fixture
def trial_off(deps: Deps) -> Deps:
    return replace(deps, presentations=FakePresentations())


def rows(messenger: FakeMessenger) -> list[list[tuple[str, str | None]]]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None, "пейволл без выхода"
    return [[(b.text, b.action) for b in row] for row in keyboard.rows]


def expected_rows(
    kind: LimitKind, label: str, action: str
) -> list[list[tuple[str, str | None]]]:
    found: list[list[tuple[str, str | None]]] = [[(label, action)]]
    invite = INVITES[kind]
    if invite is not None:
        found.append([(invite, Action.INVITE_FRIEND)])
    return found


@pytest.mark.parametrize("kind", list(LimitKind))
async def test_someone_offered_the_trial_sees_the_ruble_version(
    trial_on: Deps, session: Session, messenger: FakeMessenger, kind: LimitKind
) -> None:
    await paywall.show(trial_on, session, kind)

    assert messenger.last_text.text == WITH_TRIAL[kind]
    assert rows(messenger) == expected_rows(
        kind, "🚀 Продолжить за 1 ₽", Action.PAYWALL_TRIAL
    )


@pytest.mark.parametrize("kind", list(LimitKind))
async def test_other_free_people_see_the_lite_price_version(
    trial_off: Deps, session: Session, messenger: FakeMessenger, kind: LimitKind
) -> None:
    await paywall.show(trial_off, session, kind)

    assert messenger.last_text.text == WITHOUT_TRIAL[kind]
    assert rows(messenger) == expected_rows(
        kind, "🚀 Продолжить за 299 ₽", buy_action(TariffId.LITE.value)
    )


async def test_someone_who_paid_once_sees_the_lite_price_version(
    trial_on: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
) -> None:
    """Платил раньше — пробный период ему уже не положен, даже если тариф кончился."""
    order = await storage.create_payment(
        user_id=session.user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=299,
        currency="RUB",
        docs_version="2026-08-31",
    )
    await storage.mark_paid(order.id)

    await paywall.show(trial_on, session, LimitKind.PRESENTATIONS)

    assert messenger.last_text.text == WITHOUT_TRIAL[LimitKind.PRESENTATIONS]


def test_the_numbers_and_the_price_come_from_the_tariffs() -> None:
    """Цифры и цена — из реестра тарифов, а не вписаны руками."""
    lite = replace(
        tariff_of(TariffId.LITE),
        price_rub=399,
        monthly_presentations=12,
        daily_messages=150,
    )
    trial = replace(TRIAL, days=7, price_rub=5)

    with_price = texts.paywall_free(LimitKind.PRESENTATIONS, lite=lite, trial=None)
    with_trial = texts.paywall_free(LimitKind.PRESENTATIONS, lite=lite, trial=trial)

    assert with_price.text.splitlines()[1:] == [
        "Нужна ещё? Открой всё за 399 ₽ в месяц:",
        "12 презентаций, доклады и 150 сообщений в день.",
    ]
    assert with_trial.text.splitlines()[1] == "Нужна ещё? Открой всё на 7 дней за 5 ₽:"
    assert texts.button_continue(lite=lite, trial=trial) == "🚀 Продолжить за 5 ₽"
    assert texts.button_continue(lite=lite, trial=None) == "🚀 Продолжить за 399 ₽"


def test_no_free_paywall_says_unlimited() -> None:
    lite = tariff_of(TariffId.LITE)
    for kind in LimitKind:
        for trial in (TRIAL, None):
            assert (
                "безлимит" not in texts.paywall_free(kind, lite=lite, trial=trial).text
            )


# --- П2: куда ведут кнопки --------------------------------------------------


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


async def test_the_ruble_button_opens_the_trial_terms(
    trial_on: Deps, user: User, messenger: FakeMessenger
) -> None:
    await handle(trial_on, press(Action.PAYWALL_TRIAL))

    assert messenger.last_text.text.startswith("Пробный период «Лайт» — 3 дня за 1 ₽.")


async def test_the_price_button_opens_the_lite_order(
    trial_off: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    card_only = replace(trial_off, stars=None)

    await handle(card_only, press(buy_action(TariffId.LITE.value)))

    assert "«Лайт» — 299 ₽" in messenger.last_text.text
    (order,) = storage._payments.values()
    assert order.tariff is TariffId.LITE and order.amount == 299


async def test_an_old_ruble_button_leads_to_the_lite_order_when_the_trial_is_gone(
    trial_on: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """П2: пробный период уже недоступен — честно ведём на заказ Лайта."""
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=1,
        currency="RUB",
        docs_version="2026-08-31",
        trial=True,
    )
    await storage.mark_paid(order.id)
    card_only = replace(trial_on, stars=None)

    await handle(card_only, press(Action.PAYWALL_TRIAL))

    assert "«Лайт» — 299 ₽" in messenger.last_text.text
    lite_orders = [each for each in storage._payments.values() if each.amount == 299]
    assert len(lite_orders) == 1


# --- П3: платящим — прежние пейволлы ----------------------------------------


@pytest.mark.parametrize("kind", list(LimitKind))
async def test_a_paying_person_sees_the_old_paywall(
    trial_on: Deps,
    session: Session,
    stars: FakeStars,
    messenger: FakeMessenger,
    kind: LimitKind,
) -> None:
    await payments.start_stars(trial_on, session, TariffId.PRO)
    await payments.confirm(trial_on, stars.invoices[-1].order_id, charge_id="c-1")
    found = await trial_on.storage.get_user_by_id(session.user.id)
    assert found is not None
    pro = Session(user=found, chat=session.chat, day=session.day, now=session.now)

    await paywall.show(trial_on, pro, kind)

    assert texts.BUTTON_OPEN_TARIFFS in [b for row in rows(messenger) for b, _ in row]
    assert "бесплатн" not in messenger.last_text.text
