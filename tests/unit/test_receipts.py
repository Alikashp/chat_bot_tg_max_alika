"""Фискальный чек: что уходит онлайн-кассе и когда его не отправляют.

Проверяется здесь не красота объекта, а два обещания, за нарушение которых
платит продавец: чек уходит и с первой оплатой, и с автосписанием, а платёж
без чека не проводится вовсе.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import pending, texts
from app.core.models import Subscription, TariffId, User
from app.core.receipts import FiscalSettings, Receipt, ReceiptItem, receipt_for
from app.core.scenarios import payments, subscriptions
from app.core.scenarios.deps import Deps, Session
from app.core.settings import CoreSettings
from app.core.tariffs import RUB
from tests.fakes import FakeCards, FakeLogger

FISCAL = FiscalSettings(
    vat_code=1, payment_subject="service", payment_mode="full_payment"
)


@pytest.fixture
def fiscal_deps(deps: Deps) -> Deps:
    """Настройки с включёнными чеками — как после подключения кассы."""
    return replace(deps, settings=replace(deps.settings, fiscal=FISCAL))


# --- Сам чек -------------------------------------------------------------


def test_the_receipt_total_matches_the_payment() -> None:
    """ЮKassa сверяет их сама и на расхождение отвечает ошибкой."""
    receipt = receipt_for(
        email="alika@mail.ru",
        description="Тариф Про",
        amount_rub=599,
        currency=RUB,
        fiscal=FISCAL,
    )

    assert receipt.total_rub == 599


def test_a_receipt_without_an_address_is_refused() -> None:
    """Чек некуда отправить — значит это не чек, а тихая потеря документа."""
    with pytest.raises(ValueError):
        Receipt(
            email="",
            items=(
                ReceiptItem(
                    description="Тариф Про",
                    amount_rub=599,
                    currency=RUB,
                    vat_code=1,
                    payment_subject="service",
                    payment_mode="full_payment",
                ),
            ),
        )


def test_an_empty_receipt_is_refused() -> None:
    with pytest.raises(ValueError):
        Receipt(email="alika@mail.ru", items=())


def test_the_fiscal_values_come_from_the_settings() -> None:
    """Ставку НДС называет бухгалтер, а не код: она приходит снаружи."""
    other = FiscalSettings(
        vat_code=4, payment_subject="commodity", payment_mode="full_prepayment"
    )

    receipt = receipt_for(
        email="alika@mail.ru",
        description="Тариф Про",
        amount_rub=599,
        currency=RUB,
        fiscal=other,
    )

    assert receipt.items[0].vat_code == 4
    assert receipt.items[0].payment_subject == "commodity"
    assert receipt.items[0].payment_mode == "full_prepayment"


# --- Почта перед первой оплатой ------------------------------------------


async def test_the_first_card_payment_asks_for_an_address(
    fiscal_deps: Deps, session: Session, cards: FakeCards
) -> None:
    """Спрашиваем до заказа: с человека, уже отдавшего деньги, спрос другой."""
    await payments.start_card(fiscal_deps, session, TariffId.PRO)

    assert fiscal_deps.messenger.last_text.text == texts.EMAIL_ASK  # type: ignore[attr-defined]
    assert cards.created == [], "заказ заведён до того, как есть куда слать чек"


async def test_a_typo_in_the_address_keeps_the_purchase_alive(
    fiscal_deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Иначе опечатка выкидывала бы человека из покупки в обычный чат."""
    await storage.set_pending(session.user.id, pending.await_email("pro"))
    fresh = await storage.get_user_by_id(session.user.id)
    assert fresh is not None

    await payments.remember_email(
        fiscal_deps, replace(session, user=fresh), "это не почта"
    )

    assert fiscal_deps.messenger.last_text.text == texts.EMAIL_BAD  # type: ignore[attr-defined]
    still = await storage.get_user_by_id(session.user.id)
    assert still is not None
    assert still.email is None
    assert pending.parse_await_email(still.pending) == "pro"


async def test_a_good_address_returns_to_the_very_same_tariff(
    fiscal_deps: Deps, session: Session, storage: InMemoryStorage, cards: FakeCards
) -> None:
    """Спросив адрес, надо вернуть человека к оплате, а не в начало витрины."""
    await storage.set_pending(session.user.id, pending.await_email("max"))
    fresh = await storage.get_user_by_id(session.user.id)
    assert fresh is not None

    await payments.remember_email(
        fiscal_deps, replace(session, user=fresh), " Alika@Mail.RU "
    )

    saved = await storage.get_user_by_id(session.user.id)
    assert saved is not None
    assert saved.email == "alika@mail.ru", "адрес не приведён к нижнему регистру"
    assert saved.pending is None, "ожидание почты осталось висеть"
    assert len(cards.created) == 1
    assert cards.receipts[0] is not None
    assert cards.receipts[0].total_rub == 1490, "чек не на тот тариф"


async def test_the_address_never_reaches_the_log(
    fiscal_deps: Deps, session: Session, storage: InMemoryStorage, logger: FakeLogger
) -> None:
    """§3.5: персональные данные в логах не место. Факт согласия — можно."""
    await storage.set_pending(session.user.id, pending.await_email("pro"))
    fresh = await storage.get_user_by_id(session.user.id)
    assert fresh is not None

    await payments.remember_email(
        fiscal_deps, replace(session, user=fresh), "alika@mail.ru"
    )

    written = [event for event in logger.events if "alika@mail.ru" in str(event.fields)]
    assert written == []


async def test_a_known_address_is_not_asked_for_twice(
    fiscal_deps: Deps, session: Session, storage: InMemoryStorage, cards: FakeCards
) -> None:
    await storage.set_email(session.user.id, "alika@mail.ru")
    fresh = await storage.get_user_by_id(session.user.id)
    assert fresh is not None

    await payments.start_card(fiscal_deps, replace(session, user=fresh), TariffId.PRO)

    assert len(cards.created) == 1
    assert cards.receipts[0] is not None


async def test_the_address_is_shown_before_the_money(
    fiscal_deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Опечатку в букве человек заметит здесь, а не через месяц без чека."""
    await storage.set_email(session.user.id, "alika@mail.ru")
    fresh = await storage.get_user_by_id(session.user.id)
    assert fresh is not None

    await payments.start_card(fiscal_deps, replace(session, user=fresh), TariffId.PRO)

    assert "alika@mail.ru" in fiscal_deps.messenger.last_text.text  # type: ignore[attr-defined]


async def test_without_receipts_nothing_is_asked(
    deps: Deps, session: Session, cards: FakeCards
) -> None:
    """Чеки выключены — значит их выставляет касса без нас, и почта ни к чему."""
    await payments.start_card(deps, session, TariffId.PRO)

    assert len(cards.created) == 1
    assert cards.receipts[0] is None


# --- Автосписание --------------------------------------------------------


async def test_a_renewal_carries_its_own_receipt(
    fiscal_deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Закон не делает скидки на то, что человека нет за экраном."""
    cards = FakeCards(recurring=True)
    with_cards = replace(fiscal_deps, cards=cards)
    await storage.set_email(user.id, "alika@mail.ru")
    subscription = await _subscribed(with_cards, user)

    await subscriptions.charge(with_cards, subscription)

    assert cards.charged, "списание не прошло"
    assert cards.receipts[-1] is not None
    assert cards.receipts[-1].email == "alika@mail.ru"


async def test_a_renewal_without_an_address_takes_no_money(
    fiscal_deps: Deps, storage: InMemoryStorage, user: User, logger: FakeLogger
) -> None:
    """Штраф за недоставленный чек дороже месяца подписки.

    Попасть сюда мы не должны: почта спрашивается до первой оплаты. Но если
    попали, брать деньги нельзя — и молчать об этом тоже.
    """
    cards = FakeCards(recurring=True)
    with_cards = replace(fiscal_deps, cards=cards)
    subscription = await _subscribed(with_cards, user)

    await subscriptions.charge(with_cards, subscription)

    assert cards.charged == []
    assert [event.event for event in logger.events if event.level == "error"] == [
        "subscription_charge_without_receipt"
    ]


async def _subscribed(deps: Deps, user: User) -> Subscription:
    """Заводит подписку, которой пора списывать."""
    await deps.storage.save_subscription(_subscription(deps, user))
    found = await deps.storage.get_subscription(user.id)
    assert found is not None
    return found


def _subscription(deps: Deps, user: User) -> Subscription:
    return Subscription(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        status="active",
        amount=599,
        currency=RUB,
        next_charge_at=deps.now() - timedelta(minutes=1),
        created_at=deps.now() - timedelta(days=30),
        payment_method_id="card-1",
        # О списании уже предупредили: без этой отметки проход переносит срок
        # на сутки и напоминает вместо того, чтобы брать деньги молча.
        reminded_for=deps.now() - timedelta(minutes=1),
    )


def test_the_settings_know_whether_receipts_are_on() -> None:
    assert CoreSettings(bot_username="b").receipts_ready is False
    assert CoreSettings(bot_username="b", fiscal=FISCAL).receipts_ready is True
