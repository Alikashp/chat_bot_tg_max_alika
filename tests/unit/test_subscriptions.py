"""Подписка: что мы пообещали в оферте, то и делаем.

Пункты 4.13–4.17 — это не оформление, а обязательства, и каждое из них здесь
проверяется отдельно. Общее у них одно: все они про то, чтобы человек узнавал
о деньгах раньше банка. Проверять такое «на глаз» нельзя — молчание выглядит
в точности как исправная работа, пока кто-нибудь не посмотрит выписку.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.limits import current_day
from app.core.models import MessengerKind, Subscription, TariffId, User
from app.core.scenarios import payments, profile, subscriptions
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import RUB, STARS, tariff_of
from app.ports.payments import ChargeStatus, PaymentMethod, SubscriptionStatus
from tests.fakes import (
    FakeCards,
    FakeLogger,
    FakeMessenger,
    FakeStars,
    FrozenClock,
)

PRO = TariffId.PRO


async def _subscribe(
    deps: Deps,
    user: User,
    *,
    method: str = PaymentMethod.CARD.value,
    status: str = SubscriptionStatus.ACTIVE.value,
    amount: int = 599,
    currency: str = RUB,
    charge_at: timedelta = timedelta(days=30),
    failed_since: timedelta | None = None,
    charge_id: str | None = None,
    reminded: bool = True,
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff=PRO,
        method=method,
        status=status,
        amount=amount,
        currency=currency,
        next_charge_at=deps.now() + charge_at,
        created_at=deps.now(),
        payment_method_id="card-1" if method == PaymentMethod.CARD.value else None,
        charge_id=charge_id,
        # По умолчанию человек предупреждён: без этого списание не пройдёт
        # (§4.13 оферты), и каждый тест про деньги начинался бы с переноса.
        reminded_for=deps.now() + charge_at if reminded else None,
        failed_since=None if failed_since is None else deps.now() + failed_since,
    )
    await deps.storage.save_subscription(subscription)
    await deps.storage.set_tariff(user.id, PRO, deps.now() + charge_at)
    return subscription


async def _read(storage: InMemoryStorage, who: User) -> User:
    """Свежая запись о человеке. Отсутствовать она здесь не может."""
    found = await storage.get_user_by_id(who.id)
    assert found is not None
    return found


# --- Первая оплата заводит подписку --------------------------------------


async def test_stars_payment_starts_a_subscription(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Звёздная подписка регулярна всегда: разового счёта у неё не бывает."""
    await payments.start_stars(deps, session, PRO)

    await payments.confirm(deps, stars.invoices[0].order_id, charge_id="charge-1")

    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.method == PaymentMethod.STARS.value
    assert subscription.currency == STARS
    assert subscription.charge_id == "charge-1"
    assert subscription.next_charge_at == deps.now() + timedelta(days=30)


async def test_a_card_without_recurring_starts_no_subscription(
    deps: Deps, session: Session, storage: InMemoryStorage, cards: FakeCards
) -> None:
    """Пока автоплатежи не подключены, оплата картой — разовая.

    Завести подписку «на будущее» значило бы пообещать продление, которого не
    случится, и оставить человека без тарифа в тот день, когда он на него
    рассчитывал.
    """
    assert cards.recurring is False

    await payments.start_card(deps, session, PRO)
    await payments.confirm(deps, cards.created[0][0])

    assert await storage.get_subscription(session.user.id) is None


async def test_a_one_off_card_payment_promises_no_renewal(
    deps: Deps, session: Session, messenger: FakeMessenger, cards: FakeCards
) -> None:
    """Пообещать продление там, где его не будет, — то же, что соврать о цене."""
    assert cards.recurring is False

    await payments.start_card(deps, session, PRO)

    assert "Следующее списание" not in messenger.last_text.text
    assert "вручную" in messenger.last_text.text


async def test_a_recurring_card_payment_names_the_next_charge(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§4.4 оферты: дата ближайшего автоматического списания — до денег."""
    recurring = replace(deps, cards=FakeCards(recurring=True))

    await payments.start_card(recurring, session, PRO)

    assert "Следующее списание" in messenger.last_text.text
    assert texts.format_date(deps.today() + timedelta(days=30)) in (
        messenger.last_text.text
    )


async def test_a_recurring_card_remembers_the_saved_method(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Без сохранённого способа оплаты списывать в следующий раз будет нечем."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)

    await payments.start_card(recurring, session, PRO)
    assert cards.saved_requested is True
    await payments.confirm(recurring, cards.created[0][0])

    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None
    assert subscription.payment_method_id == "card-1"


async def test_a_card_that_was_not_saved_starts_no_subscription(
    deps: Deps, session: Session, storage: InMemoryStorage, logger: FakeLogger
) -> None:
    """Провайдер обещал запомнить карту и не запомнил — продления не будет."""
    cards = FakeCards(recurring=True)
    cards.saved_method = None
    recurring = replace(deps, cards=cards)

    await payments.start_card(recurring, session, PRO)
    await payments.confirm(recurring, cards.created[0][0])

    assert await storage.get_subscription(session.user.id) is None
    assert any(e.event == "subscription_without_method" for e in logger.events)


# --- Продление звёздами ---------------------------------------------------


async def test_a_star_renewal_extends_the_tariff(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Telegram присылает продление тем же заказом, что и первую оплату.

    Заказ к этому моменту давно оплачен, и без отдельной ветки продление
    провалилось бы как «уже подтверждённое» — вместе с оплаченным месяцем.
    """
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await payments.confirm(deps, order_id, charge_id="charge-1")
    first = (await _read(storage, session.user)).tariff_expires_at
    assert first is not None

    renewed = await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)

    assert renewed is not None
    after = (await _read(storage, session.user)).tariff_expires_at
    assert after == first + timedelta(days=30)


async def test_the_same_renewal_never_pays_twice(
    deps: Deps, session: Session, storage: InMemoryStorage, stars: FakeStars
) -> None:
    """Уведомление о списании приходит по несколько раз.

    Защита не в проверке «а не продлевали ли уже», а в самом идентификаторе
    списания: он уникален в таблице заказов, и второй раз привязать его не
    даст база.
    """
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await payments.confirm(deps, order_id, charge_id="charge-1")

    first = await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)
    second = await payments.confirm(deps, order_id, charge_id="charge-2", renewal=True)

    assert first is not None
    assert second is None


async def test_a_renewal_without_a_charge_id_grants_nothing(
    deps: Deps, session: Session, stars: FakeStars, logger: FakeLogger
) -> None:
    """Без идентификатора списания повтор от продления не отличить."""
    await payments.start_stars(deps, session, PRO)
    order_id = stars.invoices[0].order_id
    await payments.confirm(deps, order_id, charge_id="charge-1")

    assert await payments.confirm(deps, order_id, renewal=True) is None
    assert any(e.event == "renewal_without_charge_id" for e in logger.events)


# --- Экран подписки -------------------------------------------------------


async def test_without_a_subscription_the_screen_is_not_a_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await subscriptions.show(deps, session)

    assert messenger.last_text.keyboard is not None


async def test_an_active_subscription_shows_the_sum_and_the_date(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """§4.4 оферты: сумма и дата ближайшего списания должны быть видны."""
    await _subscribe(deps, user)

    await subscriptions.show(deps, session)

    assert "599 ₽" in messenger.last_text.text
    assert texts.format_date(deps.today() + timedelta(days=30)) in (
        messenger.last_text.text
    )


async def test_a_star_subscription_shows_stars(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """Списывают звёзды — значит и показывать надо звёзды, а не рубли."""
    await _subscribe(
        deps, user, method=PaymentMethod.STARS.value, amount=525, currency=STARS
    )

    await subscriptions.show(deps, session)

    assert "525 ⭐" in messenger.last_text.text


async def test_a_cancelled_subscription_says_what_is_left(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """§4.15 оферты: оплаченный период дорабатывает до конца."""
    await _subscribe(deps, user, status=SubscriptionStatus.CANCELLED.value)

    await subscriptions.show(deps, session)

    assert "Продление отключено" in messenger.last_text.text


# --- Отмена ---------------------------------------------------------------


async def test_cancelling_stops_future_charges(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    """§4.14 оферты: отменить можно в любой момент, и это вступает в силу сразу."""
    await _subscribe(deps, user)

    await subscriptions.cancel(deps, session)

    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.CANCELLED.value


async def test_cancelling_keeps_the_paid_period(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    """Отмена прекращает списания, а не отбирает оплаченное."""
    await _subscribe(deps, user)
    until = (await _read(storage, user)).tariff_expires_at

    await subscriptions.cancel(deps, session)

    after = await _read(storage, user)
    assert after.tariff is PRO
    assert after.tariff_expires_at == until


async def test_cancelling_stars_tells_telegram_first(
    deps: Deps, session: Session, user: User, stars: FakeStars
) -> None:
    """Списывает Telegram, и наша отметка его ни к чему не обязывает."""
    await _subscribe(
        deps,
        user,
        method=PaymentMethod.STARS.value,
        currency=STARS,
        amount=525,
        charge_id="charge-1",
    )

    await subscriptions.cancel(deps, session)

    assert stars.cancelled == [(user.external_id, "charge-1")]


async def test_a_failed_telegram_cancel_leaves_the_subscription_on(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    user: User,
    stars: FakeStars,
    messenger: FakeMessenger,
) -> None:
    """Худшее здесь — сказать «больше не спишем» и всё-таки списать."""
    stars.cancel_error = RuntimeError("Telegram не ответил")
    await _subscribe(
        deps,
        user,
        method=PaymentMethod.STARS.value,
        currency=STARS,
        amount=525,
        charge_id="charge-1",
    )

    await subscriptions.cancel(deps, session)

    subscription = await storage.get_subscription(user.id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert "Не вышло отключить продление" in messenger.last_text.text


async def test_cancelling_twice_is_not_an_error(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """Кнопка живёт в переписке вечно, и второе нажатие — обычное дело."""
    await _subscribe(deps, user)
    await subscriptions.cancel(deps, session)

    await subscriptions.cancel(deps, session)

    assert "Продление отключено" in messenger.last_text.text


# --- Предупреждение за сутки (§4.13) --------------------------------------


async def test_the_reminder_names_the_sum_and_the_day(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    subscription = await _subscribe(
        deps, user, charge_at=timedelta(hours=12), reminded=False
    )

    await subscriptions.remind(deps, subscription)

    when = current_day(subscription.next_charge_at, deps.settings.timezone)
    assert "599 ₽" in messenger.last_text.text
    assert texts.format_date(when) in messenger.last_text.text


async def test_the_reminder_is_marked_only_after_it_is_sent(
    deps: Deps, storage: InMemoryStorage, messenger: FakeMessenger, user: User
) -> None:
    """Пропустить обязательное предупреждение хуже, чем прислать его дважды."""
    subscription = await _subscribe(
        deps, user, charge_at=timedelta(hours=12), reminded=False
    )
    messenger.fail_send = RuntimeError("мессенджер лёг")

    with contextlib.suppress(RuntimeError):
        await subscriptions.remind(deps, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.reminded_for is None


# --- Предупреждение о новой цене (§4.17) ----------------------------------


async def test_a_changed_price_is_announced_before_it_is_charged(
    deps: Deps, storage: InMemoryStorage, messenger: FakeMessenger, user: User
) -> None:
    """Поставить перед фактом нельзя: предупредить надо за неделю."""
    subscription = await _subscribe(deps, user, amount=499)

    await subscriptions.check_price(deps, subscription)

    assert "499 ₽" in messenger.last_text.text
    assert f"{tariff_of(PRO).price_rub} ₽" in messenger.last_text.text
    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.amount == tariff_of(PRO).price_rub


async def test_an_unchanged_price_says_nothing(
    deps: Deps, storage: InMemoryStorage, messenger: FakeMessenger, user: User
) -> None:
    """Сообщение «цена та же» — это спам, а не забота."""
    subscription = await _subscribe(deps, user)

    await subscriptions.check_price(deps, subscription)

    assert messenger.texts_said() == []
    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.price_checked_for == subscription.next_charge_at


async def test_a_star_price_is_never_changed_underfoot(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    """Цену звёздной подписки держит Telegram: менять её нам нечем."""
    subscription = await _subscribe(
        deps, user, method=PaymentMethod.STARS.value, currency=STARS, amount=1
    )

    await subscriptions.check_price(deps, subscription)

    assert messenger.texts_said() == []


# --- Списание по карте ----------------------------------------------------


async def test_a_successful_charge_extends_the_tariff(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))
    before = (await _read(storage, user)).tariff_expires_at
    assert before is not None

    await subscriptions.charge(recurring, subscription)

    assert cards.charged[0][1:] == (599, "card-1")
    after = await _read(storage, user)
    assert after.tariff_expires_at == before + timedelta(days=30)
    assert "Продлили тариф" in messenger.last_text.text


async def test_a_refused_charge_is_retried_not_ended(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """§4.16 оферты: три дня попыток, а не отказ с первого раза."""
    cards = FakeCards(recurring=True)
    cards.charge_succeeds = False
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    await subscriptions.charge(recurring, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.PAST_DUE.value
    assert saved.failed_since == deps.now()
    assert saved.next_charge_at == deps.now() + timedelta(hours=24)
    assert "Не вышло списать" in messenger.last_text.text


async def test_three_days_of_refusals_end_the_subscription(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Тянуть подписку, по которой не приходят деньги, нечестно к обеим сторонам."""
    cards = FakeCards(recurring=True)
    cards.charge_succeeds = False
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(
        recurring, user, charge_at=timedelta(0), failed_since=timedelta(days=-3)
    )

    await subscriptions.charge(recurring, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.CANCELLED.value
    assert "вернули бесплатные лимиты" in messenger.last_text.text


async def test_a_provider_outage_is_not_a_refusal(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Сбой сети — это «неизвестно», а не «отказано».

    Считать его отказом значило бы наказывать человека за нашу же аварию:
    три таких сбоя подряд — и подписка кончилась, хотя банк ничего не
    отклонял.
    """
    cards = FakeCards(recurring=True)
    cards.error = RuntimeError("ЮKassa не ответила")
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    await subscriptions.charge(recurring, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.ACTIVE.value
    assert saved.failed_since is None
    assert messenger.texts_said() == []


async def test_the_paid_period_survives_a_failed_charge(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Списание не прошло — но оплаченное человек уже оплатил."""
    cards = FakeCards(recurring=True)
    cards.charge_succeeds = False
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(
        recurring, user, charge_at=timedelta(0), failed_since=timedelta(days=-3)
    )
    until = (await _read(storage, user)).tariff_expires_at

    await subscriptions.charge(recurring, subscription)

    after = await _read(storage, user)
    assert after.tariff_expires_at == until


# --- Магазин не настроен -------------------------------------------------


async def test_an_unconfigured_shop_does_not_end_the_subscription(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
    logger: FakeLogger,
) -> None:
    """Ключи магазина пропали — это наша поломка, а не отказ человека.

    Раньше подписка от этого прекращалась, а отмена ещё и стирает сохранённую
    карту. Вернуть её после починки нечем: человеку пришлось бы платить
    заново руками, и узнал бы он об этом из сообщения «подписка кончилась».
    """
    unconfigured = replace(deps, cards=None)
    subscription = await _subscribe(unconfigured, user, charge_at=timedelta(0))

    await subscriptions.charge(unconfigured, subscription)

    kept = await storage.get_subscription(user.id)
    assert kept is not None
    assert kept.status == SubscriptionStatus.ACTIVE.value
    assert kept.payment_method_id == "card-1"
    assert messenger.texts_said() == []
    assert "subscription_shop_not_configured" in logger.names()


async def test_recurring_switched_off_does_not_end_the_subscription(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    cards = FakeCards(recurring=False)
    switched_off = replace(deps, cards=cards)
    subscription = await _subscribe(switched_off, user, charge_at=timedelta(0))

    await subscriptions.charge(switched_off, subscription)

    kept = await storage.get_subscription(user.id)
    assert kept is not None
    assert kept.status == SubscriptionStatus.ACTIVE.value
    assert kept.payment_method_id == "card-1"
    assert cards.keys == []
    assert messenger.texts_said() == []


async def test_a_repaired_shop_charges_the_kept_subscription(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    subscription = await _subscribe(
        replace(deps, cards=None), user, charge_at=timedelta(0)
    )
    await subscriptions.charge(replace(deps, cards=None), subscription)

    cards = FakeCards(recurring=True)
    kept = await storage.get_subscription(user.id)
    assert kept is not None
    await subscriptions.charge(replace(deps, cards=cards), kept)

    assert cards.charged[0][1:] == (599, "card-1")


async def test_a_subscription_without_a_saved_card_ends(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Списывать нечем у самого человека — такую подписку честно прекратить."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))
    await storage.save_subscription(replace(subscription, payment_method_id=None))

    await subscriptions.charge(recurring, subscription)

    ended = await storage.get_subscription(user.id)
    assert ended is not None and ended.status == SubscriptionStatus.CANCELLED.value
    assert cards.keys == []
    assert "вернули бесплатные лимиты" in messenger.last_text.text


# --- Один период — один заказ (П8) ---------------------------------------


async def test_a_lost_answer_is_repeated_with_the_same_order(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Деньги списаны, ответ потерялся — повтор не списывает второй раз.

    Снаружи такой сбой неотличим от «провайдер не ответил». Новый заказ на
    повторе значил бы новый ключ идемпотентности, и провайдер честно списал бы
    ещё раз.
    """
    cards = FakeCards(recurring=True)
    cards.error_after_charge = TimeoutError("ответ не дошёл")
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    await subscriptions.charge(recurring, subscription)
    assert messenger.texts_said() == []

    cards.error_after_charge = None
    clock.advance(hours=1)
    due = await storage.get_subscription(user.id)
    assert due is not None and due.next_charge_at <= clock.now
    await subscriptions.charge(recurring, due)

    assert len(cards.charged) == 1
    assert len(set(cards.keys)) == 1
    # Срок истёк час назад, и период отсчитывается от момента выдачи — но
    # выдан он один раз.
    after = await _read(storage, user)
    assert after.tariff_expires_at == clock.now + timedelta(days=30)
    assert "Продлили тариф" in messenger.last_text.text


async def test_a_restart_after_the_charge_does_not_charge_again(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Перезапуск между ответом провайдера и выдачей не стоит второго списания."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    async def crash(*_: object) -> bool:
        raise RuntimeError("процесс остановлен")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "attach_external_id", crash)
        with pytest.raises(RuntimeError):
            await subscriptions.charge(recurring, subscription)

    due = await storage.get_subscription(user.id)
    assert due is not None
    await subscriptions.charge(recurring, due)

    assert len(cards.charged) == 1
    assert len(set(cards.keys)) == 1
    renewed = await storage.get_subscription(user.id)
    assert renewed is not None and renewed.charge_order_id is None
    assert renewed.next_charge_at > deps.now() + timedelta(days=29)


async def test_a_pending_charge_is_asked_about_not_repeated(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Платёж в обработке ждут, а не создают заново."""
    cards = FakeCards(recurring=True)
    cards.charge_pending = True
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    await subscriptions.charge(recurring, subscription)
    waiting = await storage.get_subscription(user.id)
    assert waiting is not None
    assert waiting.status == SubscriptionStatus.ACTIVE.value
    assert waiting.failed_since is None
    assert messenger.texts_said() == []

    cards.states["charge-1"] = ChargeStatus.CHARGED
    clock.advance(hours=1)
    await subscriptions.charge(recurring, waiting)

    assert len(cards.keys) == 1
    assert cards.status_asked == ["charge-1"]
    assert "Продлили тариф" in messenger.last_text.text


async def test_a_pending_charge_refused_later_is_a_refusal(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    cards = FakeCards(recurring=True)
    cards.charge_pending = True
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))
    await subscriptions.charge(recurring, subscription)

    cards.states["charge-1"] = ChargeStatus.REFUSED
    clock.advance(hours=1)
    waiting = await storage.get_subscription(user.id)
    assert waiting is not None
    await subscriptions.charge(recurring, waiting)

    refused = await storage.get_subscription(user.id)
    assert refused is not None
    assert refused.status == SubscriptionStatus.PAST_DUE.value
    assert refused.charge_order_id is None
    assert "Не вышло списать" in messenger.last_text.text


async def test_only_an_explicit_refusal_opens_a_new_order(
    deps: Deps, storage: InMemoryStorage, user: User, clock: FrozenClock
) -> None:
    """После отказа банка прежний заказ закрыт, и следующая попытка — новая."""
    cards = FakeCards(recurring=True)
    cards.charge_succeeds = False
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))
    await subscriptions.charge(recurring, subscription)

    cards.charge_succeeds = True
    clock.advance(days=1)
    retry = await storage.get_subscription(user.id)
    assert retry is not None
    # О повторе человек предупреждён сообщением об отказе; здесь важен
    # только заказ, с которым идёт повтор.
    await storage.mark_reminded(user.id, retry.next_charge_at)
    await subscriptions.charge(
        recurring, replace(retry, reminded_for=retry.next_charge_at)
    )

    assert len(cards.charged) == 2
    assert cards.charged[0][0] != cards.charged[1][0]


async def test_an_unknown_outcome_is_retried_inside_the_key_lifetime(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Повтор приходит, пока провайдер ещё помнит ключ.

    Раньше повтор шёл через сутки — ровно когда ЮKassa ключ забывает и
    выполняет запрос заново. То есть «повтор» мог оказаться вторым платежом.
    """
    cards = FakeCards(recurring=True)
    cards.error = RuntimeError("ЮKassa не ответила")
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))

    await subscriptions.charge(recurring, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None and saved.charge_order_id is not None
    order = await storage.get_payment(saved.charge_order_id)
    assert order is not None
    wait = saved.next_charge_at - deps.now()
    assert timedelta(0) < wait <= cards.idempotence_window / 2
    # Тот же заказ ещё и в том же виде: повтор с тем же ключом, но другими
    # данными ЮKassa отвергнет, а мы снова ничего не узнаем.
    assert saved.reminded_for == saved.next_charge_at


async def test_a_forgotten_key_is_never_reused(
    deps: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
    clock: FrozenClock,
    logger: FakeLogger,
) -> None:
    """Ключ истёк, исход неизвестен — ни повтора, ни нового заказа.

    Повтор с забытым ключом провайдер выполнит как новый платёж, новый заказ —
    тоже. Остаётся прекратить продление и оставить след для ручной сверки;
    заказ при этом не закрывается — если деньги всё-таки дошли, его закроет
    уведомление ЮKassa.
    """
    cards = FakeCards(recurring=True)
    cards.error = RuntimeError("ЮKassa не ответила")
    recurring = replace(deps, cards=cards)
    subscription = await _subscribe(recurring, user, charge_at=timedelta(0))
    await subscriptions.charge(recurring, subscription)
    calls = len(cards.keys)

    clock.advance(hours=23, minutes=30)
    cards.error = None
    stuck = await storage.get_subscription(user.id)
    assert stuck is not None
    await subscriptions.charge(recurring, stuck)

    assert len(cards.keys) == calls
    assert cards.charged == []
    ended = await storage.get_subscription(user.id)
    assert ended is not None and ended.status == SubscriptionStatus.CANCELLED.value
    assert "subscription_charge_unresolved" in logger.names()
    assert "вернули бесплатные лимиты" in messenger.last_text.text


# --- Списание по звёздам --------------------------------------------------


async def test_a_late_star_renewal_is_waited_out(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Списывает Telegram, и сообщение о нём может задержаться."""
    subscription = await _subscribe(
        deps,
        user,
        method=PaymentMethod.STARS.value,
        currency=STARS,
        amount=525,
        charge_at=timedelta(0),
        charge_id="charge-1",
    )

    await subscriptions.charge(deps, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.ACTIVE.value
    assert messenger.texts_said() == []


async def test_a_star_renewal_that_never_comes_ends_the_subscription(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Иначе отменённая в Telegram подписка вечно числилась бы действующей."""
    subscription = await _subscribe(
        deps,
        user,
        method=PaymentMethod.STARS.value,
        currency=STARS,
        amount=525,
        charge_at=timedelta(0),
        failed_since=timedelta(days=-1),
        charge_id="charge-1",
    )

    await subscriptions.charge(deps, subscription)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.CANCELLED.value
    assert "вернули бесплатные лимиты" in messenger.last_text.text


# --- Профиль --------------------------------------------------------------


async def test_the_profile_leads_to_the_subscription(
    deps: Deps, session: Session, messenger: FakeMessenger, user: User
) -> None:
    """§4.14 оферты обещает отмену «в разделе Профиль» — значит туда и ведём."""
    await _subscribe(deps, user)
    await profile.show(deps, session)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    labels = [button.text for row in keyboard.rows for button in row]
    assert texts.BUTTON_SUBSCRIPTION in labels


async def test_without_a_subscription_the_profile_stays_as_it_was(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Кнопка на экран «смотреть нечего» — это лишняя кнопка."""
    await profile.show(deps, session)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    labels = [button.text for row in keyboard.rows for button in row]
    assert texts.BUTTON_SUBSCRIPTION not in labels


# --- Кому отвечаем --------------------------------------------------------


async def test_the_answer_goes_to_the_messenger_the_person_came_from(
    deps: Deps, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Подписка лежит в общей базе, а человек — в конкретном мессенджере."""
    person = await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="max-42",
        referral_code="max42",
        support_number=support.generate_number(),
        bonus_images=3,
        bonus_documents=0,
    )
    subscription = await _subscribe(deps, person, charge_at=timedelta(hours=12))

    await subscriptions.remind(deps, subscription)

    assert messenger.last_text.chat.messenger is MessengerKind.MAX
    assert messenger.last_text.chat.chat_id == "max-42"


# --- Строка в банковской выписке ------------------------------------------


async def test_the_card_order_names_the_bank_statement_line(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Через месяц человек увидит эту строку в приложении банка.

    Незнакомая — и он звонит оспаривать списание. Показанная до оплаты, она
    этот звонок предотвращает.
    """
    await payments.start_card(deps, session, PRO)

    assert deps.settings.bank_statement_name in messenger.last_text.text


async def test_a_star_order_says_nothing_about_banks(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """У звёзд списывает мессенджер: банковской выписки не будет вовсе."""
    await payments.start_stars(deps, session, PRO)

    assert deps.settings.bank_statement_name not in messenger.last_text.text


# --- Отмена во время прохода ----------------------------------------------


async def test_a_cancellation_during_the_pass_is_not_undone(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Проход держит копию подписки, прочитанную в его начале.

    Пока он идёт — а идёт он с обращениями к провайдеру и мессенджеру, —
    человек успевает нажать «Отключить продление». Записать обратно свою
    копию значит воскресить отменённую подписку и списать с человека в
    следующем месяце деньги, от которых он отказался.
    """
    cards = FakeCards(recurring=True)
    cards.charge_succeeds = False
    recurring = replace(deps, cards=cards)
    stale = await _subscribe(recurring, user, charge_at=timedelta(0))
    await storage.cancel_subscription(user.id, deps.now())

    await subscriptions.charge(recurring, stale)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.CANCELLED.value


async def test_money_is_not_taken_from_someone_who_just_cancelled(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Худшее в этой гонке — не испорченная строка, а списанные деньги."""
    cards = FakeCards(recurring=True)
    recurring = replace(deps, cards=cards)
    stale = await _subscribe(recurring, user, charge_at=timedelta(0))
    await storage.cancel_subscription(user.id, deps.now())

    await subscriptions.charge(recurring, stale)

    assert cards.charged == []


async def test_a_price_change_does_not_revive_a_cancelled_subscription(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """То же самое в проходе сверки цен."""
    stale = await _subscribe(deps, user, charge_at=timedelta(days=6), amount=499)
    await storage.cancel_subscription(user.id, deps.now())

    await subscriptions.check_price(deps, stale)

    saved = await storage.get_subscription(user.id)
    assert saved is not None
    assert saved.status == SubscriptionStatus.CANCELLED.value
