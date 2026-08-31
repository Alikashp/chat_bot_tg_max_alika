"""Обход подписок: порядок проходов, изоляция сбоев и выбор мессенджера.

Проверяется здесь ровно то, чего не видно в сценариях по отдельности:
предупреждаем ли мы раньше, чем списываем, и не роняет ли одна испорченная
подписка остальные. И то и другое — про чужие деньги, поэтому «наверное,
работает» тут не годится.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support
from app.core.billing import Billing
from app.core.models import MessengerKind, Subscription, TariffId, User
from app.core.scenarios.deps import Deps
from app.core.tariffs import RUB
from app.ports.payments import PaymentMethod, SubscriptionStatus
from tests.fakes import FakeCards, FakeLogger, FakeMessenger

PRO = TariffId.PRO


async def _subscribe(
    deps: Deps,
    user: User,
    *,
    charge_at: timedelta,
    amount: int = 599,
    reminded: bool = True,
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff=PRO,
        method=PaymentMethod.CARD.value,
        status=SubscriptionStatus.ACTIVE.value,
        amount=amount,
        currency=RUB,
        next_charge_at=deps.now() + charge_at,
        created_at=deps.now(),
        payment_method_id="card-1",
        # По умолчанию человек предупреждён: без этого списание не пройдёт, и
        # каждый тест про деньги начинался бы с переноса срока.
        reminded_for=deps.now() + charge_at if reminded else None,
    )
    await deps.storage.save_subscription(subscription)
    await deps.storage.set_tariff(user.id, PRO, deps.now() + charge_at)
    return subscription


def _billing(deps: Deps) -> Billing:
    return Billing(by_messenger={MessengerKind.TELEGRAM: deps})


async def test_a_charge_due_today_is_taken(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await _subscribe(recurring, user, charge_at=timedelta(0))

    await _billing(recurring).run()

    assert len(cards.charged) == 1


async def test_a_charge_that_is_not_due_is_left_alone(deps: Deps, user: User) -> None:
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await _subscribe(recurring, user, charge_at=timedelta(days=10))

    await _billing(recurring).run()

    assert cards.charged == []


async def test_the_warning_comes_before_the_money(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Предупреждение вдогонку списанию — это не предупреждение.

    За сутки до срока проход обязан сказать о деньгах и обязан их не брать:
    иначе человек узнаёт о списании от банка, а не от нас.
    """
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await _subscribe(recurring, user, charge_at=timedelta(hours=12), reminded=False)

    await _billing(recurring).run()

    assert cards.charged == []
    assert "Завтра" in messenger.texts_said()[0]


async def test_a_price_change_is_announced_a_week_ahead(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """§4.17 оферты: за семь дней, а не в день списания."""
    await _subscribe(deps, user, charge_at=timedelta(days=6), amount=499)

    await _billing(deps).check_prices()

    assert "499 ₽" in messenger.texts_said()[0]


async def test_a_price_change_further_out_can_wait(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    await _subscribe(deps, user, charge_at=timedelta(days=20), amount=499)

    await _billing(deps).check_prices()

    assert messenger.texts_said() == []


async def test_one_broken_subscription_does_not_stop_the_rest(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    logger: FakeLogger,
    messenger: FakeMessenger,
) -> None:
    """Проход идёт по чужим деньгам: чужой сбой не должен стоить их владельцу.

    Ломаем первую подписку так, чтобы шаг по ней бросил исключение: способа
    оплаты нет, значит она прекращается, а сообщить об этом мессенджер
    откажется. Без изоляции этот сбой унёс бы с собой и вторую подписку.
    """
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    second = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="2",
        referral_code="code2",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )
    await _subscribe(recurring, user, charge_at=timedelta(0))
    await _subscribe(recurring, second, charge_at=timedelta(0))
    broken = await storage.get_subscription(user.id)
    assert broken is not None
    await storage.save_subscription(replace(broken, payment_method_id=None))
    messenger.fail_send = RuntimeError("мессенджер лёг")

    await _billing(recurring).charge()

    assert [charge[2] for charge in cards.charged] == ["card-1"]
    assert any(e.event == "billing_step_failed" for e in logger.events)


async def test_a_subscription_from_a_disabled_messenger_is_skipped(
    deps: Deps, storage: InMemoryStorage, logger: FakeLogger
) -> None:
    """Взять деньги и не суметь об этом сказать хуже, чем не взять."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    person = await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="max-1",
        referral_code="max1",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )
    await _subscribe(recurring, person, charge_at=timedelta(0))

    await Billing(by_messenger={MessengerKind.TELEGRAM: recurring}).charge()

    assert cards.charged == []
    assert any(e.event == "billing_messenger_disabled" for e in logger.events)


def test_billing_needs_at_least_one_messenger() -> None:
    """Без единого мессенджера обход некому и отвечать — значит, он бессмыслен."""
    with pytest.raises(ValueError, match="мессенджер"):
        Billing(by_messenger={})


async def test_a_charge_nobody_was_warned_about_is_deferred(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """§4.13 оферты: предупредить обязаны, и обязаны заранее.

    Сюда попадают, только если проход напоминаний проспал своё окно — сервис
    лежал сутки. Списать молча дешевле для нас и дороже для доверия, поэтому
    срок переносится, а человек получает то самое предупреждение.
    """
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await _subscribe(recurring, user, charge_at=timedelta(0), reminded=False)

    await _billing(recurring).charge()

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert cards.charged == []
    assert saved.next_charge_at == deps.now() + timedelta(hours=24)
    assert saved.reminded_for == saved.next_charge_at
    assert "Завтра" in messenger.texts_said()[-1]


async def test_the_deferred_charge_goes_through_next_time(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Перенос — это отсрочка, а не отмена: через сутки деньги берутся."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    await _subscribe(recurring, user, charge_at=timedelta(0), reminded=False)
    await _billing(recurring).charge()

    later = replace(recurring, now=lambda: deps.now() + timedelta(hours=25))
    await _billing(later).charge()

    assert len(cards.charged) == 1
