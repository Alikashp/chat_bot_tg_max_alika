"""Подтверждение оплаты по требованию (сессия 14, шаг 2, О2).

В бою 15 из 18 оплат по 1 ₽ подтвердились сверкой через 10–14 минут. Теперь,
если у человека есть неоплаченный заказ со ссылкой, кнопка покупки, профиль
и тарифы сразу спрашивают ЮKassa об этом платеже. Оплачен — заказ проводится
обычным подтверждением, и человек видит включённый тариф. Выдача — одна,
кто бы ни успел первым: уведомление, сверка или этот вопрос.
"""

from __future__ import annotations

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
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps
from tests.fakes import FakeCards, FakeLogger, FakeMessenger, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


@pytest.fixture
def cards() -> FakeCards:
    found = FakeCards(recurring=True)
    found.paid = False
    return found


@pytest.fixture
def shop(deps: Deps, cards: FakeCards) -> Deps:
    return replace(
        deps,
        cards=cards,
        settings=replace(deps.settings, trial_enabled=True, stars_enabled=False),
    )


def trial_on(messenger: FakeMessenger) -> bool:
    return any(sent.text.startswith("Готово! Пробный") for sent in messenger.texts)


async def fresh(storage: InMemoryStorage, user: User) -> User:
    found = await storage.get_user_by_id(user.id)
    assert found is not None
    return found


@pytest.mark.parametrize(
    "action",
    [
        Action.TRIAL,
        Action.PAYWALL_TRIAL,
        buy_action("pro"),
        method_action("card", "lite"),
    ],
    ids=["trial", "paywall", "other-tariff", "card-button"],
)
async def test_a_purchase_button_shows_the_paid_trial_instead_of_a_link(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    action: str,
) -> None:
    """Оплатил, уведомления ещё нет — любая кнопка покупки проводит оплату."""
    await handle(shop, press(Action.TRIAL))
    cards.paid = True
    clock.advance(minutes=2)
    created = len(cards.created)

    await handle(shop, press(action))

    assert trial_on(messenger)
    assert len(cards.created) == created
    found = await fresh(storage, user)
    assert found.tariff is TariffId.LITE and found.trial_order_id is not None


@pytest.mark.parametrize(
    ("action", "screen"),
    [
        (Action.MENU_PROFILE, "Твой тариф"),
        (Action.MENU_TARIFFS, texts.tariffs_screen(with_presentations=False).text),
        (Action.OPEN_TARIFFS, texts.tariffs_screen(with_presentations=False).text),
    ],
    ids=["profile", "tariffs", "tariffs-from-a-message"],
)
async def test_profile_and_tariffs_show_the_paid_tariff_at_once(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
    action: str,
    screen: str,
) -> None:
    await handle(shop, press(Action.TRIAL))
    cards.paid = True
    clock.advance(minutes=2)

    await handle(shop, press(action))

    assert trial_on(messenger)
    assert screen[:10] in messenger.last_text.text
    assert (await fresh(storage, user)).tariff is TariffId.LITE


async def test_a_full_price_payment_is_confirmed_by_the_tariffs_button(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """О4: то же для Лайта за полную цену."""
    no_trial = replace(shop, settings=replace(shop.settings, trial_enabled=False))
    await handle(no_trial, press(buy_action("lite")))
    cards.paid = True

    await handle(no_trial, press(buy_action("lite")))

    assert any(sent.text.startswith("Готово") for sent in messenger.texts)
    assert len(cards.created) == 1
    assert (await fresh(storage, user)).tariff is TariffId.LITE


async def test_the_grant_stays_single_whoever_comes_first(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
) -> None:
    """Подтвердили по требованию — опоздавшее уведомление и второе нажатие
    ничего не добавляют."""
    await handle(shop, press(Action.TRIAL))
    (order,) = storage.orders_of(user.id)
    cards.paid = True
    await handle(shop, press(Action.MENU_PROFILE))
    granted = (await fresh(storage, user)).tariff_expires_at

    clock.advance(minutes=10)
    assert await payments.confirm(shop, order.id) is None
    await handle(shop, press(Action.MENU_PROFILE))

    assert (await fresh(storage, user)).tariff_expires_at == granted
    assert [sent.text.startswith("Готово! Пробный") for sent in messenger.texts].count(
        True
    ) == 1


async def test_an_unpaid_order_changes_nothing_on_the_profile(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    await handle(shop, press(Action.TRIAL))

    await handle(shop, press(Action.MENU_PROFILE))

    assert not trial_on(messenger)
    assert (await fresh(storage, user)).tariff is TariffId.FREE
    assert cards.asked == ["ext-1"]


async def test_only_live_links_are_asked_about(
    shop: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    clock: FrozenClock,
) -> None:
    """Старше срока жизни ссылки — дело сверки, а не каждого открытия профиля."""
    await handle(shop, press(Action.TRIAL))
    clock.advance(minutes=shop.settings.payment_link_ttl_minutes + 1)

    await handle(shop, press(Action.MENU_PROFILE))

    assert cards.asked == []


async def test_a_silent_provider_still_shows_the_profile(
    shop: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
    logger: FakeLogger,
) -> None:
    await handle(shop, press(Action.TRIAL))

    async def silent(external_id: str, *, expected_rub: int) -> bool:
        raise ConnectionError("ЮKassa молчит")

    cards.is_paid = silent  # type: ignore[method-assign]

    await handle(shop, press(Action.MENU_PROFILE))

    assert "Твой тариф" in messenger.last_text.text
    assert "payment_check_failed" in repr(logger.events)


async def test_paying_and_then_fixing_the_email_shows_the_trial(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Почта — не кнопка покупки, но и по ней второй ссылки нет: живой
    заказ оказался оплачен — проводим его."""
    fiscal = replace(
        shop,
        settings=replace(
            shop.settings,
            fiscal=FiscalSettings(
                vat_code=1, payment_subject="service", payment_mode="full_payment"
            ),
        ),
    )
    await storage.set_email(user.id, "first@mail.ru")
    await handle(fiscal, press(Action.TRIAL))
    await handle(fiscal, press(email_action(TRIAL_TARGET)))
    cards.paid = True

    await handle(
        fiscal, IncomingMessage(chat=CHAT, external_user_id="1", text="new@mail.ru")
    )

    assert trial_on(messenger)
    assert len(cards.created) == 1


async def test_the_profile_names_the_tariff_just_confirmed(
    shop: Deps,
    user: User,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    await handle(shop, press(Action.TRIAL))
    cards.paid = True

    await handle(shop, press(Action.MENU_PROFILE))

    assert "Лайт" in messenger.last_text.text


async def test_a_paid_full_price_lite_is_not_followed_by_a_trial_link(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
) -> None:
    """Купил Лайт за полную цену, уведомления нет, жмёт «за 1 ₽» на пейволле:
    проводим Лайт, а не выдаём ссылку на пробный период."""
    await handle(shop, press(method_action("card", "lite")))
    cards.paid = True

    await handle(shop, press(Action.PAYWALL_TRIAL))

    assert len(cards.created) == 1
    assert (await fresh(storage, user)).tariff is TariffId.LITE


async def test_every_paid_live_link_is_settled(
    shop: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    cards: FakeCards,
    logger: FakeLogger,
) -> None:
    """Две живые ссылки на разные покупки, оплачены обе: обе проведены —
    лишний 1 ₽ по-прежнему виден в логе (trial_payment_unused)."""
    await handle(shop, press(method_action("card", "lite")))
    await handle(shop, press(Action.TRIAL))
    cards.paid = True

    await handle(shop, press(Action.MENU_PROFILE))

    assert {order.status for order in storage.orders_of(user.id)} == {"paid"}
    assert "trial_payment_unused" in repr(logger.events)
