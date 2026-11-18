"""Telegram: оплата только звёздами (сессия 15, шаг 1, З1–З2).

Telegram предупредил: цифровые товары в ботах можно продавать только за
звёзды, иначе блокировка без предупреждения. Поэтому продажа картой в
Telegram выключается настройкой, а не удалением ключей: ключи нужны, чтобы
довести уже созданные заказы, и с ними должно оставаться так же безопасно,
как без них.

Каждый тест здесь — с ключами магазина Telegram на месте, пробным периодом
и чеками включёнными: худший случай, при котором прежний код повёл бы к
карте.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import pending
from app.core.actions import (
    TRIAL_TARGET,
    Action,
    buy_action,
    email_action,
    method_action,
)
from app.core.limits import LimitKind
from app.core.models import (
    Chat,
    IncomingMessage,
    MessengerKind,
    Subscription,
    TariffId,
    User,
)
from app.core.receipts import FiscalSettings
from app.core.router import handle
from app.core.scenarios import paywall
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import RUB, stars_price, tariff_of
from app.ports.payments import PaymentMethod, SubscriptionStatus
from tests.fakes import FakeCards, FakeMessenger, FakeStars

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
#: Цены в звёздах при настройках по умолчанию (наценка 1,4, 1,6 ₽ за звезду).
DEFAULT_STARS = {TariffId.LITE: 262, TariffId.PRO: 525, TariffId.MAX: 1304}


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


@pytest.fixture
def cards() -> FakeCards:
    found = FakeCards(recurring=True)
    found.paid = False
    return found


@pytest.fixture
def telegram(deps: Deps, cards: FakeCards) -> Deps:
    """Telegram с ключами магазина, пробным периодом и чеками — и без карты."""
    return replace(
        deps,
        cards=cards,
        settings=replace(
            deps.settings,
            card_sales_enabled=False,
            trial_enabled=True,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )


def seen(messenger: FakeMessenger) -> str:
    """Всё, что человек увидел: тексты и подписи кнопок."""
    parts: list[str] = []
    for sent in messenger.texts:
        parts.append(sent.text)
        if sent.keyboard is not None:
            parts.extend(button.text for row in sent.keyboard.rows for button in row)
    return "\n".join(parts)


def assert_no_card(messenger: FakeMessenger, cards: FakeCards) -> None:
    shown = seen(messenger)
    assert cards.created == [], "платёж ЮKassa не создаётся"
    assert "₽" not in shown, shown
    assert "💳" not in shown and "картой" not in shown.lower(), shown
    assert "почт" not in shown.lower(), shown


def test_default_prices_in_stars() -> None:
    """Формула прежняя: цена в рублях × наценка ÷ курс, с округлением вверх."""
    for tariff_id, expected in DEFAULT_STARS.items():
        assert stars_price(tariff_of(tariff_id), markup=1.4, rub_per_star=1.6) == (
            expected
        )


@pytest.mark.parametrize("action", [Action.MENU_TARIFFS, Action.OPEN_TARIFFS])
async def test_the_tariffs_screen_shows_stars_and_no_trial(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    action: str,
) -> None:
    await handle(telegram, press(action))

    shown = seen(messenger)
    assert_no_card(messenger, cards)
    for price in DEFAULT_STARS.values():
        assert f"{price} ⭐/мес" in shown
    assert "Попробовать" not in shown


@pytest.mark.parametrize(
    "action",
    [
        buy_action("lite"),
        buy_action("pro"),
        buy_action("max"),
        method_action("card", "pro"),
        method_action("stars", "pro"),
        Action.PAYWALL_TRIAL,
        email_action("pro"),
    ],
    ids=["lite", "pro", "max", "old-card", "stars", "paywall-trial", "other-email"],
)
async def test_every_purchase_button_leads_to_stars(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
    action: str,
) -> None:
    """Включая старые кнопки из переписок: «Картой», «за 1 ₽», «Другая почта»."""
    await handle(telegram, press(action))

    assert_no_card(messenger, cards)
    (invoice,) = stars.invoices
    assert invoice.stars in DEFAULT_STARS.values()
    assert f"{invoice.stars} ⭐" in messenger.last_text.text


@pytest.mark.parametrize(
    "action",
    [Action.TRIAL, email_action(TRIAL_TARGET)],
    ids=["trial", "trial-email"],
)
async def test_old_trial_buttons_lead_to_the_tariffs(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    action: str,
) -> None:
    await handle(telegram, press(action))

    assert_no_card(messenger, cards)
    assert "262 ⭐/мес" in messenger.last_text.text


async def test_the_card_screen_is_not_offered_even_with_both_methods(
    telegram: Deps, user: User, messenger: FakeMessenger, cards: FakeCards
) -> None:
    """Звёзды и ключи магазина есть оба — выбора способа всё равно нет."""
    await handle(telegram, press(buy_action("pro")))

    assert "Как удобнее платить" not in seen(messenger)


async def test_without_stars_on_sale_nothing_is_sold_by_card(
    telegram: Deps, user: User, messenger: FakeMessenger, cards: FakeCards
) -> None:
    """Звёзды выключены — значит, покупки нет вовсе, а не карта взамен."""
    no_stars = replace(
        telegram, settings=replace(telegram.settings, stars_enabled=False)
    )

    for action in (buy_action("pro"), method_action("card", "lite"), Action.TRIAL):
        await handle(no_stars, press(action))

    assert cards.created == []


@pytest.mark.parametrize(
    "kind",
    [
        LimitKind.PRESENTATIONS,
        LimitKind.IMAGES,
        LimitKind.DOCUMENTS,
        LimitKind.MESSAGES,
    ],
)
async def test_the_free_paywall_names_the_price_in_stars(
    telegram: Deps,
    session: Session,
    messenger: FakeMessenger,
    cards: FakeCards,
    kind: LimitKind,
) -> None:
    await paywall.show(telegram, session, kind)

    assert_no_card(messenger, cards)
    sent = messenger.last_text
    assert "Открой всё за 262 ⭐ в месяц:" in sent.text
    assert sent.keyboard is not None
    button = sent.keyboard.rows[0][0]
    assert button.text == "🚀 Продолжить за 262 ⭐"
    assert button.action == buy_action("lite")


async def test_an_email_awaited_before_the_switch_starts_no_card_payment(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
) -> None:
    """Почту спросили до выключения, ответ пришёл после: карты нет, а
    сообщение не теряется — оно уходит в обычный чат."""
    await storage.set_pending(user.id, pending.await_email("pro"))

    await handle(
        telegram, IncomingMessage(chat=CHAT, external_user_id="1", text="a@mail.ru")
    )

    assert_no_card(messenger, cards)
    assert stars.invoices == [], "это не покупка, а обычное сообщение"
    found = await storage.get_user_by_id(user.id)
    assert found is not None and found.pending is None
    assert found.email is None, "почту, которая больше не нужна, не храним"


@pytest.mark.parametrize(
    "action",
    [method_action("card", "pro"), Action.TRIAL, Action.PAYWALL_TRIAL],
    ids=["old-card", "trial", "paywall-trial"],
)
async def test_a_known_email_does_not_open_the_card_either(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    action: str,
) -> None:
    """Почта уже есть — вопроса о ней нет, и ничто не задерживает путь к
    карте. Его не должно быть и тут."""
    await storage.set_email(user.id, "a@mail.ru")

    await handle(telegram, press(action))

    assert cards.created == []
    assert "₽" not in seen(messenger)


async def _card_subscription(deps: Deps, user: User, *, trial: bool = False) -> None:
    await deps.storage.save_subscription(
        Subscription(
            user_id=user.id,
            tariff=TariffId.LITE,
            method=PaymentMethod.CARD.value,
            status=SubscriptionStatus.ACTIVE.value,
            amount=299,
            currency=RUB,
            next_charge_at=deps.now() + timedelta(days=10),
            created_at=deps.now(),
            payment_method_id="card-1",
            remind_before_charge=trial,
        )
    )
    await deps.storage.set_tariff(
        user.id, TariffId.LITE, deps.now() + timedelta(days=10)
    )


async def test_a_card_subscription_does_not_block_buying_stars(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    stars: FakeStars,
) -> None:
    """Карточная подписка в Telegram больше не продлится, так что звёзды —
    не вторая подписка, а замена ей."""
    await _card_subscription(telegram, user)

    await handle(telegram, press(buy_action("lite")))

    assert len(stars.invoices) == 1
    assert_no_card(messenger, cards)


@pytest.mark.parametrize("action", [Action.SUBSCRIPTION, Action.MENU_PROFILE])
async def test_a_card_subscription_is_shown_as_ending(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    action: str,
) -> None:
    """Ни «следующее списание 299 ₽», ни «новый месяц с …»: срок дорабатывает."""
    await _card_subscription(telegram, user)

    await handle(telegram, press(action))

    shown = seen(messenger)
    assert "₽" not in shown and "списание" not in shown, shown
    assert "до " in messenger.last_text.text
    assert "новый месяц" not in shown


async def test_the_subscription_screen_offers_stars(
    telegram: Deps, user: User, messenger: FakeMessenger, stars: FakeStars
) -> None:
    await _card_subscription(telegram, user)
    await handle(telegram, press(Action.SUBSCRIPTION))
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    actions = [button.action for row in keyboard.rows for button in row]
    assert buy_action("lite") in actions
    assert "262 ⭐" in messenger.last_text.text

    await handle(telegram, press(buy_action("lite")))

    assert len(stars.invoices) == 1
