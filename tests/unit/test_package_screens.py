"""Экраны пакетов Telegram (сессия 16, шаг 3, П4, П6).

Экран тарифов — пакеты по образцу соседнего бота; пейволл бесплатного ведёт
к недельному пакету; профиль называет пакет и срок; за сутки до конца —
предложение купить снова, после конца — бесплатные нормы.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core.actions import Action, buy_action
from app.core.billing import Billing
from app.core.limits import LimitKind
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.router import handle
from app.core.scenarios import paywall
from app.core.scenarios.deps import Deps, Session
from tests.fakes import FakeMessenger, FakePresentations, FakeStars, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")


@pytest.fixture
def telegram(deps: Deps) -> Deps:
    return replace(
        deps,
        cards=None,
        settings=replace(
            deps.settings, card_sales_enabled=False, packages_enabled=True
        ),
    )


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def buttons(messenger: FakeMessenger) -> list[tuple[str, str | None]]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [(button.text, button.action) for row in keyboard.rows for button in row]


async def own(deps: Deps, user: User, package: TariffId, *, ends_in: timedelta) -> None:
    await deps.storage.set_tariff(user.id, package, deps.now() + ends_in)


# --- Экран пакетов ---------------------------------------------------------


@pytest.mark.parametrize("action", [Action.MENU_TARIFFS, Action.OPEN_TARIFFS])
async def test_the_tariffs_screen_shows_the_three_packages(
    telegram: Deps, user: User, messenger: FakeMessenger, action: str
) -> None:
    with_decks = replace(telegram, presentations=FakePresentations())

    await handle(with_decks, press(action))

    shown = messenger.last_text.text
    for line in (
        "⚡️ Неделя — 149 ⭐ на 7 дней",
        "🚀 Месяц — 349 ⭐ на 30 дней",
        "💥 Полгода — 1490 ⭐ на 180 дней",
        "· 100 сообщений в день",
        "· 200 сообщений в день",
        "· 5 картинок",
        "· 3 презентации",
        "· 90 докладов",
        "Без автопродления",
        "@PremiumBot",
        "Во всех пакетах",
    ):
        assert line in shown, line
    assert "₽" not in shown and "Лайт" not in shown and "/мес" not in shown
    assert buttons(messenger) == [
        ("Купить Неделю — 149 ⭐", buy_action("week")),
        ("Купить Месяц — 349 ⭐", buy_action("month")),
        ("Купить Полгода — 1490 ⭐", buy_action("halfyear")),
    ]


async def test_without_the_presentations_api_there_is_no_word_of_them(
    telegram: Deps, user: User, messenger: FakeMessenger
) -> None:
    assert telegram.presentations is None

    await handle(telegram, press(Action.MENU_TARIFFS))

    assert "презентац" not in messenger.last_text.text.lower()


async def test_a_package_button_leads_to_its_one_off_invoice(
    telegram: Deps, user: User, messenger: FakeMessenger, stars: FakeStars
) -> None:
    await handle(telegram, press(Action.MENU_TARIFFS))
    _, action = buttons(messenger)[1]
    assert action is not None

    await handle(telegram, press(action))

    (invoice,) = stars.invoices
    assert invoice.stars == 349 and invoice.period_days is None


# --- Пейволл бесплатного ---------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        LimitKind.PRESENTATIONS,
        LimitKind.IMAGES,
        LimitKind.DOCUMENTS,
        LimitKind.MESSAGES,
    ],
)
async def test_the_free_paywall_leads_to_the_week_package(
    telegram: Deps,
    session: Session,
    messenger: FakeMessenger,
    stars: FakeStars,
    kind: LimitKind,
) -> None:
    await paywall.show(telegram, session, kind)

    sent = messenger.last_text
    assert "Открой всё на 7 дней за 149 ⭐:" in sent.text
    assert "₽" not in sent.text and "в месяц" not in sent.text
    first, *_ = buttons(messenger)
    assert first == ("🚀 Продолжить за 149 ⭐", buy_action("week"))

    await handle(telegram, press(buy_action("week")))
    assert stars.invoices[-1].stars == 149


async def test_the_presentation_paywall_names_the_week_numbers(
    telegram: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await paywall.show(telegram, session, LimitKind.PRESENTATIONS)

    assert "3 презентации, доклады и 100 сообщений в день." in messenger.last_text.text


# --- Профиль ---------------------------------------------------------------


async def test_the_profile_names_the_package_its_end_and_what_is_left(
    telegram: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    await own(telegram, user, TariffId.WEEK, ends_in=timedelta(days=7))

    await handle(telegram, press(Action.MENU_PROFILE))

    text = messenger.last_text.text
    assert text.startswith("Твой пакет: Неделя · до ")
    assert "Сообщений сегодня: 0 из 100" in text
    # 5 картинок пакета и 3 бонусных при регистрации.
    assert "Картинки: 8" in text


async def test_after_the_end_the_profile_is_free_again(
    telegram: Deps, user: User, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    await own(telegram, user, TariffId.WEEK, ends_in=timedelta(days=7))
    clock.advance(days=7, minutes=1)

    await handle(telegram, press(Action.MENU_PROFILE))

    text = messenger.last_text.text
    assert text.startswith("Твой тариф: Бесплатный")
    assert "Сообщений сегодня: 0 из 20" in text


# --- За сутки до конца -----------------------------------------------------


def billing(deps: Deps) -> Billing:
    return Billing(by_messenger={MessengerKind.TELEGRAM: deps})


async def test_a_day_before_the_end_buying_again_is_offered_once(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    stars: FakeStars,
    clock: FrozenClock,
) -> None:
    await own(telegram, user, TariffId.WEEK, ends_in=timedelta(hours=12))

    await billing(telegram).run()
    await billing(telegram).run()

    (sent,) = messenger.texts
    assert sent.text.startswith("Завтра, ")
    assert "закончится пакет «Неделя»" in sent.text
    assert buttons(messenger)[0] == ("🔁 Купить снова — 149 ⭐", buy_action("week"))

    clock.advance(hours=13)
    await billing(telegram).run()
    assert len(messenger.texts) == 1, "после конца — ни продления, ни сообщений"
    assert stars.invoices == []


async def test_no_notice_far_from_the_end(
    telegram: Deps, user: User, messenger: FakeMessenger
) -> None:
    await own(telegram, user, TariffId.MONTH, ends_in=timedelta(days=3))

    await billing(telegram).run()

    assert messenger.texts == []


async def test_a_package_bought_again_is_noticed_again_at_its_new_end(
    telegram: Deps, user: User, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Отметка — по сроку: новый срок — новое предупреждение."""
    await own(telegram, user, TariffId.WEEK, ends_in=timedelta(hours=12))
    await billing(telegram).run()
    await own(telegram, user, TariffId.WEEK, ends_in=timedelta(days=7))
    clock.advance(days=6, hours=12)

    await billing(telegram).run()

    assert len(messenger.texts) == 2


async def test_where_packages_are_not_sold_no_notice_goes(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    await own(deps, user, TariffId.WEEK, ends_in=timedelta(hours=12))

    await billing(deps).run()

    assert messenger.texts == []
