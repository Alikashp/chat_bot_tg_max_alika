"""MAX после сессии 15: карта, пробный период, чеки и автопродление как были (З5).

Запрет Telegram на продажу не за звёзды MAX не касается. Настройка карты в
Telegram на MAX не влияет — проверяется здесь вместе с Telegram в одном
проходе биллинга: каждый мессенджер живёт по своим правилам.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support
from app.core.actions import Action, buy_action
from app.core.billing import Billing
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
from app.core.tariffs import RUB
from app.ports.payments import PaymentMethod, SubscriptionStatus
from tests.fakes import FakeCards, FakeMessenger

CHAT = Chat(messenger=MessengerKind.MAX, chat_id="m1")


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="m1", action=action)


@pytest.fixture
def max_cards() -> FakeCards:
    found = FakeCards(recurring=True)
    found.paid = False
    return found


@pytest.fixture
def max_deps(deps: Deps, max_cards: FakeCards) -> Deps:
    """MAX: своя ЮKassa с автоплатежами, звёзд нет, карта продаётся."""
    return replace(
        deps,
        cards=max_cards,
        stars=None,
        settings=replace(
            deps.settings,
            card_sales_enabled=True,
            trial_enabled=True,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )


@pytest.fixture
async def max_user(storage: InMemoryStorage) -> User:
    return await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="m1",
        referral_code="codem1",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )


async def test_the_tariffs_are_in_rubles_with_the_trial(
    max_deps: Deps, max_user: User, messenger: FakeMessenger
) -> None:
    await handle(max_deps, press(Action.MENU_TARIFFS))

    shown = "\n".join(sent.text for sent in messenger.texts)
    assert "299 ₽/мес" in shown and "599 ₽/мес" in shown
    assert "⭐" not in shown
    assert "за 1 ₽" in shown


async def test_the_trial_is_paid_by_card_with_a_receipt(
    max_deps: Deps,
    max_user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    max_cards: FakeCards,
) -> None:
    await handle(max_deps, press(Action.TRIAL))
    assert "почту" in messenger.last_text.text

    await handle(
        max_deps,
        IncomingMessage(chat=CHAT, external_user_id="m1", text="m@mail.ru"),
    )

    (created,) = max_cards.created
    assert created[1] == 1
    assert "Пробный период" in messenger.last_text.text


async def test_a_tariff_is_paid_by_card(
    max_deps: Deps,
    max_user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    max_cards: FakeCards,
) -> None:
    await storage.set_email(max_user.id, "m@mail.ru")

    await handle(max_deps, press(buy_action("pro")))

    (created,) = max_cards.created
    assert created[1] == 599
    assert "599 ₽ каждые 30 дней" in messenger.last_text.text


async def test_the_free_paywall_offers_the_trial_for_1_rub(
    max_deps: Deps, max_user: User, messenger: FakeMessenger
) -> None:
    session = Session(
        user=max_user, chat=CHAT, day=max_deps.today(), now=max_deps.now()
    )

    await paywall.show(max_deps, session, LimitKind.PRESENTATIONS)

    assert messenger.last_text.keyboard is not None
    assert messenger.last_text.keyboard.rows[0][0].text == "🚀 Продолжить за 1 ₽"


async def _card_subscription(
    deps: Deps, user: User, *, ends_in: timedelta, trial: bool
) -> None:
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
            created_at=deps.now() - timedelta(days=30),
            payment_method_id="card-1",
            remind_before_charge=trial,
        )
    )
    await deps.storage.set_tariff(user.id, TariffId.LITE, ends)
    await deps.storage.set_email(user.id, "m@mail.ru")


async def test_one_billing_pass_charges_max_and_spares_telegram(
    deps: Deps,
    max_deps: Deps,
    max_user: User,
    user: User,
    storage: InMemoryStorage,
    max_cards: FakeCards,
) -> None:
    max_cards.paid = True
    telegram_cards = FakeCards(recurring=True)
    telegram = replace(
        deps,
        cards=telegram_cards,
        settings=replace(deps.settings, card_sales_enabled=False),
    )
    await _card_subscription(
        max_deps, max_user, ends_in=-timedelta(minutes=1), trial=False
    )
    await _card_subscription(telegram, user, ends_in=-timedelta(minutes=1), trial=False)

    await Billing(
        by_messenger={MessengerKind.TELEGRAM: telegram, MessengerKind.MAX: max_deps}
    ).run()

    assert len(max_cards.charged) == 1
    assert telegram_cards.charged == []
    renewed = await storage.get_subscription(max_user.id)
    assert renewed is not None and renewed.status == SubscriptionStatus.ACTIVE.value


async def test_max_still_warns_before_the_first_299_after_the_trial(
    max_deps: Deps, max_user: User, messenger: FakeMessenger
) -> None:
    await _card_subscription(
        max_deps, max_user, ends_in=timedelta(hours=12), trial=True
    )

    await Billing(by_messenger={MessengerKind.MAX: max_deps}).run()

    (sent,) = messenger.texts
    assert "спишем 299 ₽" in sent.text
    assert "не продлится" not in sent.text


async def test_an_ordinary_max_renewal_gets_no_end_notice(
    max_deps: Deps, max_user: User, messenger: FakeMessenger
) -> None:
    """Обычное продление в MAX — без напоминаний, как и раньше."""
    await _card_subscription(
        max_deps, max_user, ends_in=timedelta(hours=12), trial=False
    )

    await Billing(by_messenger={MessengerKind.MAX: max_deps}).run()

    assert messenger.texts == []


async def test_without_the_trial_the_paywall_names_the_ruble_price(
    max_deps: Deps, max_user: User, messenger: FakeMessenger
) -> None:
    no_trial = replace(
        max_deps, settings=replace(max_deps.settings, trial_enabled=False)
    )
    session = Session(
        user=max_user, chat=CHAT, day=no_trial.today(), now=no_trial.now()
    )

    await paywall.show(no_trial, session, LimitKind.PRESENTATIONS)

    assert "Открой всё за 299 ₽ в месяц:" in messenger.last_text.text
