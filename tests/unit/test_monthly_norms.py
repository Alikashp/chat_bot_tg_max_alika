"""Месячная норма — третий вид нормы рядом с дневной и бонусом (фаза 11, часть 2).

Здесь проверяется сама механика: откуда берётся период, когда он
обновляется, в каком порядке тратятся корзины и что остаётся после конца
оплаченного срока. Сами числа тарифов — в test_tariffs.py, чтобы проверки
механики не зависели от того, сколько картинок в каком тарифе.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from app.adapters.storage.memory import InMemoryStorage
from app.core.limits import (
    Allowance,
    LimitKind,
    NormPeriod,
    Source,
    monthly_norm,
    norm_period,
)
from app.core.models import Chat, MessengerKind, Payment, TariffId, User, UserId
from app.core.scenarios import payments, spending, subscriptions
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import tariff_of
from tests.fakes import FakeCards, FakeLogger, FakeStars, FrozenClock

CREATED = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
MONTH = timedelta(days=30)
PRO = TariffId.PRO


def _user(**fields: object) -> User:
    base = User(
        id=UserId(1),
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        tariff=TariffId.FREE,
        referral_code="code",
        support_number=123456,
        created_at=CREATED,
    )
    return replace(base, **fields)  # type: ignore[arg-type]


def _period(user: User, now: datetime) -> NormPeriod:
    return norm_period(user, now, free_days=30, paid_days=30)


# --- Период --------------------------------------------------------------


def test_the_free_period_runs_thirty_days_from_registration() -> None:
    period = _period(_user(), CREATED + timedelta(days=10))

    assert period == NormPeriod(start=CREATED, end=CREATED + MONTH)


def test_the_free_period_rolls_over_every_thirty_days() -> None:
    period = _period(_user(), CREATED + timedelta(days=65))

    assert period == NormPeriod(start=CREATED + 2 * MONTH, end=CREATED + 3 * MONTH)


def test_the_paid_period_starts_with_the_payment() -> None:
    paid = CREATED + timedelta(days=5, hours=3)
    user = _user(tariff=PRO, tariff_expires_at=paid + MONTH, norm_since=paid)

    period = _period(user, paid + timedelta(days=3))

    assert period == NormPeriod(start=paid, end=paid + MONTH)


def test_the_paid_period_never_outlives_the_term() -> None:
    """Оплачено полтора месяца — второй период кончается вместе со сроком."""
    paid = CREATED + timedelta(days=5)
    expires = paid + timedelta(days=45)
    user = _user(tariff=PRO, tariff_expires_at=expires, norm_since=paid)

    period = _period(user, paid + timedelta(days=35))

    assert period == NormPeriod(start=paid + MONTH, end=expires)


def test_a_subscriber_from_before_the_norms_counts_from_the_term() -> None:
    """Подписчик, оплативший до выкладки, отметки о начале периода не имеет.

    Его период — последние тридцать дней перед концом срока: ровно то, за
    что он заплатил.
    """
    expires = CREATED + timedelta(days=20)
    user = _user(tariff=PRO, tariff_expires_at=expires, norm_since=None)

    period = _period(user, CREATED + timedelta(days=2))

    assert period == NormPeriod(start=expires - MONTH, end=expires)


def test_after_the_term_the_free_period_applies() -> None:
    """Т5: оплаченный срок кончился — и период, и норма снова бесплатные."""
    paid = CREATED + timedelta(days=5)
    user = _user(tariff=PRO, tariff_expires_at=paid + MONTH, norm_since=paid)

    period = _period(user, paid + MONTH + timedelta(hours=1))

    assert period == NormPeriod(start=CREATED + MONTH, end=CREATED + 2 * MONTH)


# --- Порядок корзин ------------------------------------------------------


def test_the_norm_is_spent_before_the_bonus() -> None:
    left = Allowance(
        kind=LimitKind.IMAGES,
        daily_limit=0,
        daily_used=0,
        bonus=2,
        monthly_limit=5,
        monthly_used=4,
    )

    assert left.next_source is Source.MONTHLY
    assert left.total_left == 3
    assert replace(left, monthly_used=5).next_source is Source.BONUS
    assert replace(left, monthly_used=5, bonus=0).exhausted


# --- Сценарий: списание и обновление -------------------------------------


def _session(deps: Deps, user: User) -> Session:
    return Session(
        user=user,
        chat=Chat(messenger=user.messenger, chat_id=user.external_id),
        day=deps.today(),
        now=deps.now(),
    )


async def _fresh(deps: Deps, user: User) -> Session:
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None
    return _session(deps, found)


async def _subscribe(
    deps: Deps, session: Session, stars: FakeStars, tariff: TariffId = PRO
) -> Payment:
    await payments.start_stars(deps, session, tariff)
    order = await payments.confirm(
        deps, stars.invoices[-1].order_id, charge_id=f"charge-{len(stars.invoices)}"
    )
    assert order is not None
    return order


async def _use(deps: Deps, session: Session, kind: LimitKind, times: int) -> None:
    for _ in range(times):
        await spending.charge(deps, session, kind)


async def test_the_norm_is_spent_first_and_the_bonus_after(
    deps: Deps, session: Session, stars: FakeStars, storage: InMemoryStorage
) -> None:
    """Т2: бонус — продолжение работы после нормы, а не её замена."""
    await _subscribe(deps, session, stars)
    current = await _fresh(deps, session.user)
    norm = monthly_norm(current.tariff, LimitKind.IMAGES)
    await storage.add_bonus(current.user.id, images=2)
    bonus = (await spending.current_allowance(deps, current, LimitKind.IMAGES)).bonus

    await _use(deps, current, LimitKind.IMAGES, norm)

    left = await spending.current_allowance(deps, current, LimitKind.IMAGES)
    assert (left.monthly_left, left.bonus) == (0, bonus)
    await _use(deps, current, LimitKind.IMAGES, 1)
    left = await spending.current_allowance(deps, current, LimitKind.IMAGES)
    assert left.bonus == bonus - 1


async def test_the_norm_renews_with_each_renewal_and_does_not_carry_over(
    deps: Deps, session: Session, stars: FakeStars, clock: FrozenClock
) -> None:
    """Т2: продление начинает новый период с полной нормой — не больше и не меньше."""
    order = await _subscribe(deps, session, stars)
    current = await _fresh(deps, session.user)
    norm = monthly_norm(current.tariff, LimitKind.IMAGES)
    await _use(deps, current, LimitKind.IMAGES, 3)

    clock.advance(days=30)
    await payments.confirm(deps, order.id, charge_id="renewal-1", renewal=True)

    renewed = await _fresh(deps, session.user)
    left = await spending.current_allowance(deps, renewed, LimitKind.IMAGES)
    assert left.monthly_left == norm


async def test_a_payment_mid_period_starts_the_norm_over(
    deps: Deps, session: Session, stars: FakeStars, clock: FrozenClock
) -> None:
    """Норма обновляется с каждой оплатой, а не раз в календарный месяц."""
    await _subscribe(deps, session, stars)
    current = await _fresh(deps, session.user)
    await _use(deps, current, LimitKind.DOCUMENTS, 5)
    clock.advance(hours=1)

    await _subscribe(deps, current, stars, TariffId.MAX)

    upgraded = await _fresh(deps, session.user)
    left = await spending.current_allowance(deps, upgraded, LimitKind.DOCUMENTS)
    assert left.monthly_used == 0
    assert left.monthly_left == monthly_norm(upgraded.tariff, LimitKind.DOCUMENTS)


async def test_the_bonus_survives_a_new_period(
    deps: Deps,
    session: Session,
    stars: FakeStars,
    clock: FrozenClock,
    storage: InMemoryStorage,
) -> None:
    """Т2: подарок за друга не сгорает вместе с месяцем."""
    order = await _subscribe(deps, session, stars)
    await storage.add_bonus(session.user.id, images=4)
    current = await _fresh(deps, session.user)
    before = (await spending.current_allowance(deps, current, LimitKind.IMAGES)).bonus

    clock.advance(days=30)
    await payments.confirm(deps, order.id, charge_id="renewal-1", renewal=True)

    renewed = await _fresh(deps, session.user)
    left = await spending.current_allowance(deps, renewed, LimitKind.IMAGES)
    assert left.bonus == before >= 4


async def test_the_norm_is_not_charged_into_the_next_period(
    deps: Deps, session: Session, stars: FakeStars, clock: FrozenClock
) -> None:
    """Потраченное в прошлом периоде не уменьшает нового."""
    order = await _subscribe(deps, session, stars)
    current = await _fresh(deps, session.user)
    await _use(deps, current, LimitKind.IMAGES, 1)
    before = await spending.current_allowance(deps, current, LimitKind.IMAGES)

    clock.advance(days=30)
    await payments.confirm(deps, order.id, charge_id="renewal-1", renewal=True)

    renewed = await _fresh(deps, session.user)
    after = await spending.current_allowance(deps, renewed, LimitKind.IMAGES)
    assert before.monthly_used == 1
    assert after.monthly_used == 0


# --- Т5: после оплаченного — бесплатное ----------------------------------


def _free_norms(left: Allowance) -> bool:
    return left.monthly_limit == monthly_norm(
        tariff_of(TariffId.FREE), LimitKind(left.kind)
    )


async def test_after_the_term_the_free_norms_apply(
    deps: Deps, session: Session, stars: FakeStars, clock: FrozenClock
) -> None:
    await _subscribe(deps, session, stars)

    clock.advance(days=30, minutes=1)

    later = await _fresh(deps, session.user)
    for kind in (LimitKind.IMAGES, LimitKind.DOCUMENTS, LimitKind.PRESENTATIONS):
        assert _free_norms(await spending.current_allowance(deps, later, kind))


async def test_after_a_cancelled_term_the_free_norms_apply(
    deps: Deps, session: Session, stars: FakeStars, clock: FrozenClock
) -> None:
    """Отмена не отбирает оплаченного (§4.15 оферты), но и не продлевает его."""
    await _subscribe(deps, session, stars)
    current = await _fresh(deps, session.user)
    await subscriptions.cancel(deps, current)

    still = await spending.current_allowance(deps, current, LimitKind.IMAGES)
    assert still.monthly_limit == monthly_norm(tariff_of(PRO), LimitKind.IMAGES)

    clock.advance(days=30, minutes=1)
    later = await _fresh(deps, session.user)
    assert _free_norms(await spending.current_allowance(deps, later, LimitKind.IMAGES))


async def test_a_refund_ends_the_paid_norm(
    deps: Deps, session: Session, storage: InMemoryStorage, logger: FakeLogger
) -> None:
    """Т5: деньги за месяц вернули — месяц с его нормой кончается сейчас."""
    cards = FakeCards(recurring=True)
    paid = replace(deps, cards=cards)
    await payments.start_card(paid, session, PRO)
    order = await payments.confirm(paid, cards.created[0][0])
    assert order is not None
    assert await storage.mark_refunded(order.id)

    await payments.refunded(paid, order)

    later = await _fresh(paid, session.user)
    assert later.tariff.id is TariffId.FREE
    assert _free_norms(await spending.current_allowance(paid, later, LimitKind.IMAGES))
    subscription = await storage.get_subscription(session.user.id)
    assert subscription is not None and subscription.status == "cancelled"


async def test_a_refund_of_an_early_renewal_takes_back_only_that_month(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Оплатил следующий месяц заранее и вернул его — текущий остаётся."""
    cards = FakeCards(recurring=True)
    paid = replace(deps, cards=cards)
    await payments.start_card(paid, session, PRO)
    await payments.confirm(paid, cards.created[0][0])
    first = await _fresh(paid, session.user)
    await payments.start_card(paid, first, PRO)
    second = await payments.confirm(paid, cards.created[1][0])
    assert second is not None
    await storage.mark_refunded(second.id)

    await payments.refunded(paid, second)

    later = await _fresh(paid, session.user)
    assert later.tariff.id is PRO
    assert later.user.tariff_expires_at == deps.now() + MONTH


async def test_a_refund_of_a_long_gone_month_changes_nothing(
    deps: Deps, session: Session, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    """Возврат за месяц, который давно прошёл, не отбирает оплаченного другим."""
    cards = FakeCards(recurring=True)
    paid = replace(deps, cards=cards)
    await payments.start_card(paid, session, PRO)
    old = await payments.confirm(paid, cards.created[0][0])
    assert old is not None
    clock.advance(days=31)
    current = await _fresh(paid, session.user)
    await payments.start_card(paid, current, PRO)
    await payments.confirm(paid, cards.created[1][0])
    expires = (await _fresh(paid, session.user)).user.tariff_expires_at

    await payments.refunded(paid, old)

    later = await _fresh(paid, session.user)
    assert later.tariff.id is PRO
    assert later.user.tariff_expires_at == expires


def test_period_dates_are_whole_days_for_people() -> None:
    """Дата конца периода нужна человеку днём, а не моментом."""
    period = _period(_user(), CREATED + timedelta(days=1))

    assert period.end.date() == date(2026, 8, 31)
