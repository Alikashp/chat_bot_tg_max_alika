"""Подарок из R2 (сессия 12, шаг 4, Р7, Р8).

Одна презентация начисляется только после успешной доставки R2 и действует
3 суток от отправки. Тратится первой: подарок со сроком, затем месячная
норма, затем бессрочные бонусы. Неиспользованный сгорает; в базе видно —
начислен, использован, сгорел.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import gifts, support, texts
from app.core.actions import Action
from app.core.limits import Allowance, LimitKind, Source
from app.core.mailing import Mailer
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.router import handle
from app.core.scenarios import spending
from app.core.scenarios.deps import Deps, session_for
from app.core.scenarios.reach import WatchedMessenger
from app.ports.messenger import RecipientGoneError
from tests.fakes import FakeLogger, FakeMessenger, FakePresentations, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="7")


@pytest.fixture
def presentations() -> FakePresentations:
    return FakePresentations()


@pytest.fixture
def on(deps: Deps, presentations: FakePresentations) -> Deps:
    return replace(
        deps,
        presentations=presentations,
        settings=replace(
            deps.settings,
            mailings_enabled=True,
            mailing_r1_enabled=True,
            mailing_r2_enabled=True,
            mailing_r3_enabled=True,
        ),
    )


async def quiet(storage: InMemoryStorage) -> User:
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="7",
        referral_code="code7",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )


async def r2_sent(on: Deps, storage: InMemoryStorage, clock: FrozenClock) -> User:
    """Человек молчит 10 суток с часом — R2 уходит в 16:00 по Москве."""
    user = await quiet(storage)
    clock.advance(days=10, hours=1)
    await Mailer(deps=on).run()
    return user


async def build(on: Deps, messenger: FakeMessenger) -> None:
    """Тема (бот её ждёт после R2) → «Собрать»."""
    await handle(
        on, IncomingMessage(chat=CHAT, external_user_id="7", text="Фотосинтез")
    )
    action = next(
        button.action
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
        if button.text == texts.BUTTON_DECK_BUILD
    )
    await handle(on, IncomingMessage(chat=CHAT, external_user_id="7", action=action))


async def left(on: Deps, user: User) -> Allowance:
    fresh = await on.storage.get_user_by_id(user.id)
    assert fresh is not None
    return await spending.current_allowance(
        on, session_for(on, fresh), LimitKind.PRESENTATIONS
    )


# --- Начисление ---------------------------------------------------------------


async def test_a_delivered_r2_grants_one_presentation_for_three_days(
    on: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await r2_sent(on, storage, clock)

    (gift,) = await storage.gifts_of(user.id)
    assert (gift.kind, gift.source) == (LimitKind.PRESENTATIONS, "R2")
    assert gift.granted_at == clock.now
    assert gift.expires_at == clock.now + timedelta(days=3)
    assert gift.used_at is None and gift.expired_at is None
    assert (await left(on, user)).total_left == 1


async def test_r1_and_r3_grant_nothing(
    on: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await Mailer(deps=on).run()
    clock.advance(days=18)
    await Mailer(deps=on).run()

    assert [row.step for row in await storage.mailings_of(user.id)] == ["R1", "R3"]
    assert await storage.gifts_of(user.id) == []


async def test_a_blocked_r2_grants_nothing(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    logger: FakeLogger,
) -> None:
    """Р7: заблокировал бота — отметка, статус blocked, подарка нет."""
    messenger.fail_send = RecipientGoneError("bot was blocked by the user")
    watched = replace(
        on,
        messenger=WatchedMessenger(
            messenger, storage=storage, logger=logger, now=clock
        ),
    )

    user = await r2_sent(watched, storage, clock)

    assert [(r.step, r.status.value) for r in await storage.mailings_of(user.id)] == [
        ("R2", "blocked")
    ]
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.stopped_at is not None
    assert await storage.gifts_of(user.id) == []


async def test_a_failed_r2_grants_nothing(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    messenger.fail_send = ConnectionError("сеть")

    user = await r2_sent(on, storage, clock)

    assert await storage.gifts_of(user.id) == []


# --- Порядок трат --------------------------------------------------------------


def test_the_gift_goes_first_then_the_norm_then_the_bonus() -> None:
    def source(gift: int, monthly: int, bonus: int) -> Source | None:
        return Allowance(
            kind=LimitKind.PRESENTATIONS,
            daily_limit=0,
            daily_used=0,
            bonus=bonus,
            monthly_limit=monthly,
            gift=gift,
        ).next_source

    assert source(1, 1, 1) is Source.GIFT
    assert source(0, 1, 1) is Source.MONTHLY
    assert source(0, 0, 1) is Source.BONUS
    assert source(1, 0, 0) is Source.GIFT
    assert source(0, 0, 0) is None


async def test_the_gift_is_spent_before_the_bonus(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    presentations: FakePresentations,
) -> None:
    """Р8: бессрочный бонус есть — а тратится подарок со сроком."""
    user = await r2_sent(on, storage, clock)
    await storage.add_bonus(user.id, presentations=1)

    await build(on, messenger)

    assert len(presentations.requests) == 1
    (gift,) = await storage.gifts_of(user.id)
    assert gift.used_at == clock.now
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.bonus_presentations == 1


async def test_the_gift_is_spent_before_the_monthly_norm(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Получил подарок, потом оплатил Лайт — первой уходит подарок."""
    user = await r2_sent(on, storage, clock)
    await storage.set_tariff(user.id, TariffId.LITE, clock.now + timedelta(days=30))
    before = (await left(on, user)).monthly_left

    await build(on, messenger)

    (gift,) = await storage.gifts_of(user.id)
    assert gift.used_at is not None
    assert (await left(on, user)).monthly_left == before


async def test_the_gift_is_spent_once(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    presentations: FakePresentations,
) -> None:
    """Подарок одна презентация: вторая — уже пейволл."""
    user = await r2_sent(on, storage, clock)
    await build(on, messenger)
    clock.advance(minutes=5)
    await handle(
        on,
        IncomingMessage(
            chat=CHAT, external_user_id="7", action=Action.PRESENTATION_AGAIN
        ),
    )

    assert (await left(on, user)).total_left == 0


# --- Сгорание -------------------------------------------------------------------


async def test_an_unused_gift_burns_after_three_days(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    presentations: FakePresentations,
    logger: FakeLogger,
) -> None:
    """Р8: через 3 суток подарок не тратится, а проход отмечает «сгорел»."""
    user = await r2_sent(on, storage, clock)
    sent = clock.now
    clock.advance(days=3, minutes=-1)
    assert (await left(on, user)).total_left == 1

    clock.advance(minutes=1)
    assert (await left(on, user)).total_left == 0
    await build(on, messenger)
    assert presentations.requests == []

    await gifts.expire(storage, logger, clock.now)

    (gift,) = await storage.gifts_of(user.id)
    assert gift.used_at is None
    assert gift.expired_at == sent + timedelta(days=3)


async def test_a_used_gift_does_not_burn(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    logger: FakeLogger,
) -> None:
    user = await r2_sent(on, storage, clock)
    await build(on, messenger)
    clock.advance(days=4)

    await gifts.expire(storage, logger, clock.now)

    (gift,) = await storage.gifts_of(user.id)
    assert gift.used_at is not None and gift.expired_at is None


@pytest.mark.parametrize("paid", [False, True], ids=["bonus", "norm"])
async def test_a_gift_taken_in_parallel_falls_back_in_order(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    paid: bool,
) -> None:
    """Подарок увели между проверкой и списанием — списывается следующее по
    порядку: норма, если она есть, иначе бонус."""
    user = await r2_sent(on, storage, clock)
    await storage.add_bonus(user.id, presentations=1)
    if paid:
        await storage.set_tariff(user.id, TariffId.LITE, clock.now + timedelta(days=30))
    before = await left(on, user)

    async def taken(*_: object) -> bool:
        return False

    storage.spend_gift = taken  # type: ignore[method-assign,assignment]
    await build(on, messenger)

    after = await left(on, user)
    if paid:
        assert (after.monthly_left, after.bonus) == (
            before.monthly_left - 1,
            before.bonus,
        )
    else:
        assert after.bonus == before.bonus - 1
