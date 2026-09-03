"""Контрактные тесты порта Storage.

Один и тот же набор прогоняется по каждой реализации хранилища. На фазе 1
это InMemoryStorage; на фазе 3 в параметризацию добавляется PostgreSQL, и
именно эти тесты доказывают, что реализации взаимозаменяемы.

Тесты проверяют не только «работает», но и гарантии, на которые опирается
продуктовая логика: атомарность списания и идемпотентность рефералки.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core import support
from app.core.models import (
    NO_USERNAME,
    ChatTurn,
    DialogState,
    MessengerKind,
    Role,
    Subscription,
    TariffId,
    User,
)
from app.ports.storage import Storage

DAY = date(2026, 8, 28)
NEXT_DAY = date(2026, 8, 29)

#: Момент, от которого считаются сроки подписки. С зоной: наивное время
#: PostgreSQL примет, а сравнить с ним потом не даст.
MOMENT = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


#: Куда ходить за настоящей базой. Без переменной тесты по PostgreSQL
#: пропускаются: у разработчика может не быть под рукой сервера, а вот в CI
#: он поднимается сервисом, и там пропусков быть не должно.
TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine | None]:
    """Движок и таблицы для тестов по настоящей базе.

    Движок создаётся на каждый тест, а не один на прогон: соединения asyncpg
    привязаны к своей петле событий, а у каждого теста она своя. Общий движок
    падал бы на втором тесте с невнятной ошибкой про закрытую петлю.
    """
    if TEST_DSN is None:
        # Не skip: эту фикстуру запрашивают все параметры, включая память,
        # и пропуск здесь унёс бы с собой и её тесты.
        yield None
        return
    engine = create_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        # Чистим перед тестом, а не после: если предыдущий упал, его мусор не
        # должен утащить за собой следующий.
        await connection.execute(
            text(
                "TRUNCATE subscriptions, payments, referrals, dialogs, usage, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest, postgres_engine: AsyncEngine | None
) -> AsyncIterator[Storage]:
    """Хранилище под тестом.

    Один и тот же набор тестов гоняется по обеим реализациям. В памяти
    атомарность получается сама собой — внутри операции нет ни одного await.
    В PostgreSQL её обеспечивают ограничения схемы и блокировки строк, и
    именно поэтому параллельные тесты здесь не формальность.
    """
    if request.param == "memory":
        yield InMemoryStorage()
        return

    if request.param == "postgres":
        if postgres_engine is None:
            pytest.skip("TEST_DATABASE_URL не задан")
        yield PostgresStorage(postgres_engine)
        return

    raise AssertionError(f"неизвестная реализация хранилища: {request.param}")


async def _make_user(storage: Storage, external_id: str = "1") -> User:
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id=external_id,
        referral_code=f"code{external_id}",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )


# --- Пользователи --------------------------------------------------------


async def test_created_user_is_found_by_external_id(storage: Storage) -> None:
    created = await _make_user(storage, "42")

    found = await storage.get_user(MessengerKind.TELEGRAM, "42")

    assert found is not None
    assert found.id == created.id
    assert found.tariff is TariffId.FREE
    assert found.daily_image_quota == 3
    assert found.bonus_messages == 0
    assert found.bonus_images == 0


async def test_unknown_user_is_none(storage: Storage) -> None:
    assert await storage.get_user(MessengerKind.TELEGRAM, "нет-такого") is None


async def test_same_external_id_in_other_messenger_is_another_user(
    storage: Storage,
) -> None:
    """Один и тот же числовой id в Telegram и в MAX — разные люди."""
    telegram_user = await _make_user(storage, "7")
    max_user = await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="7",
        referral_code="code-max-7",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )

    assert telegram_user.id != max_user.id


async def test_a_second_registration_returns_the_same_person(
    storage: Storage,
) -> None:
    """Два первых обновления от нового человека приходят почти одновременно.

    Оба видят «его ещё нет» и оба заводят. Раньше второй получал ошибку, и
    человек вместо ответа видел «что-то пошло не так» на первом же касании.
    """
    first = await _make_user(storage, "twice")
    second = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="twice",
        referral_code="другой-код",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )

    assert second.id == first.id
    assert second.referral_code == first.referral_code, "код менять нельзя"


async def test_concurrent_registrations_create_one_person(storage: Storage) -> None:
    """То же самое, но параллельно — как оно и происходит в жизни.

    Написан ради PostgreSQL: в памяти внутри операции нет ни одного await, и
    гонки не получается. В базе её разрешает ON CONFLICT DO NOTHING.
    """
    people = await asyncio.gather(
        *(
            storage.create_user(
                messenger=MessengerKind.TELEGRAM,
                external_id="race",
                referral_code=f"code-{index}",
                support_number=support.generate_number(),
                daily_image_quota=3,
            )
            for index in range(5)
        )
    )

    assert len({person.id for person in people}) == 1


async def test_duplicate_referral_code_is_rejected(storage: Storage) -> None:
    await _make_user(storage, "1")

    with pytest.raises(ValueError):
        await storage.create_user(
            messenger=MessengerKind.TELEGRAM,
            external_id="2",
            referral_code="code1",
            support_number=support.generate_number(),
            daily_image_quota=3,
        )


async def test_user_is_found_by_referral_code(storage: Storage) -> None:
    created = await _make_user(storage, "9")

    found = await storage.get_user_by_referral_code("code9")

    assert found is not None
    assert found.id == created.id


async def test_unknown_referral_code_is_none(storage: Storage) -> None:
    assert await storage.get_user_by_referral_code("нет") is None


async def test_tariff_can_be_changed(storage: Storage) -> None:
    user = await _make_user(storage)
    expires = datetime(2026, 9, 30, tzinfo=UTC)

    await storage.set_tariff(user.id, TariffId.PRO, expires)

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.tariff is TariffId.PRO
    assert updated.tariff_expires_at == expires


# --- Дневной расход ------------------------------------------------------


async def test_usage_starts_at_zero(storage: Storage) -> None:
    """«Ничего не тратил» и «нет записи» — одно и то же."""
    user = await _make_user(storage)

    usage = await storage.get_usage(user.id, DAY)

    assert usage.messages_used == 0
    assert usage.images_used == 0
    assert usage.day == DAY


async def test_usage_accumulates(storage: Storage) -> None:
    user = await _make_user(storage)

    await storage.add_usage(user.id, DAY, messages=1)
    await storage.add_usage(user.id, DAY, messages=1, images=1)

    usage = await storage.get_usage(user.id, DAY)
    assert usage.messages_used == 2
    assert usage.images_used == 1


async def test_usage_is_isolated_per_day(storage: Storage) -> None:
    """Смена суток — это новая запись, а не фоновый сброс счётчика."""
    user = await _make_user(storage)
    await storage.add_usage(user.id, DAY, messages=5)

    assert (await storage.get_usage(user.id, NEXT_DAY)).messages_used == 0
    assert (await storage.get_usage(user.id, DAY)).messages_used == 5


async def test_usage_is_isolated_per_user(storage: Storage) -> None:
    first = await _make_user(storage, "1")
    second = await _make_user(storage, "2")

    await storage.add_usage(first.id, DAY, messages=3)

    assert (await storage.get_usage(second.id, DAY)).messages_used == 0


async def test_concurrent_usage_increments_are_not_lost(storage: Storage) -> None:
    """Двадцать параллельных списаний должны дать ровно двадцать.

    Без атомарности здесь теряются инкременты, и пользователь получает
    больше, чем ему полагается.
    """
    user = await _make_user(storage)

    await asyncio.gather(
        *(storage.add_usage(user.id, DAY, messages=1) for _ in range(20))
    )

    assert (await storage.get_usage(user.id, DAY)).messages_used == 20


# --- Бонусный баланс -----------------------------------------------------


async def test_bonus_is_added_and_spent(storage: Storage) -> None:
    user = await _make_user(storage)

    await storage.add_bonus(user.id, messages=50, images=5)
    assert await storage.spend_bonus(user.id, images=1) is True

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_messages == 50
    assert updated.bonus_images == 4


async def test_spending_more_bonus_than_available_changes_nothing(
    storage: Storage,
) -> None:
    """Списание — всё или ничего: частичного не бывает."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, messages=1, images=1)

    assert await storage.spend_bonus(user.id, messages=1, images=2) is False

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_messages == 1
    assert updated.bonus_images == 1


async def test_bonus_cannot_be_overspent_concurrently(storage: Storage) -> None:
    """Десять параллельных попыток при балансе 3 дают ровно 3 успеха."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, images=3)

    results = await asyncio.gather(
        *(storage.spend_bonus(user.id, images=1) for _ in range(10))
    )

    assert sum(results) == 3
    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_images == 0


# --- Диалог --------------------------------------------------------------


async def test_dialog_starts_empty(storage: Storage) -> None:
    user = await _make_user(storage)

    dialog = await storage.get_dialog(user.id)

    assert dialog.turns == ()
    assert dialog.user_turns == 0


async def test_dialog_is_saved_and_read_back(storage: Storage) -> None:
    user = await _make_user(storage)
    dialog = DialogState(
        turns=(ChatTurn(Role.USER, "привет"), ChatTurn(Role.ASSISTANT, "здравствуй")),
        user_turns=1,
    )

    await storage.save_dialog(user.id, dialog)

    assert await storage.get_dialog(user.id) == dialog


async def test_dialog_reset_clears_history_and_counter(storage: Storage) -> None:
    user = await _make_user(storage)
    await storage.save_dialog(
        user.id, DialogState(turns=(ChatTurn(Role.USER, "привет"),), user_turns=9)
    )

    await storage.reset_dialog(user.id)

    dialog = await storage.get_dialog(user.id)
    assert dialog.turns == ()
    assert dialog.user_turns == 0


# --- Рефералы ------------------------------------------------------------


async def test_referral_is_recorded_once(storage: Storage) -> None:
    referrer = await _make_user(storage, "1")
    referee = await _make_user(storage, "2")

    assert await storage.record_referral(referrer.id, referee.id) is True


async def test_repeated_referral_is_rejected(storage: Storage) -> None:
    """Повторный /start по той же ссылке не должен начислять ничего.

    Это гарантия хранилища, а не аккуратности вызывающего кода
    (в PostgreSQL — ограничение уникальности).
    """
    referrer = await _make_user(storage, "1")
    referee = await _make_user(storage, "2")
    await storage.record_referral(referrer.id, referee.id)

    assert await storage.record_referral(referrer.id, referee.id) is False
    assert await storage.count_referrals(referrer.id) == 1


async def test_self_referral_is_rejected(storage: Storage) -> None:
    user = await _make_user(storage)

    assert await storage.record_referral(user.id, user.id) is False
    assert await storage.count_referrals(user.id) == 0


async def test_referee_cannot_be_counted_twice_for_different_referrers(
    storage: Storage,
) -> None:
    """Один приглашённый приносит награду только одному пригласившему."""
    first = await _make_user(storage, "1")
    second = await _make_user(storage, "2")
    referee = await _make_user(storage, "3")

    assert await storage.record_referral(first.id, referee.id) is True
    assert await storage.record_referral(second.id, referee.id) is False


async def test_concurrent_referral_records_only_one_wins(storage: Storage) -> None:
    referrer = await _make_user(storage, "1")
    referee = await _make_user(storage, "2")

    results = await asyncio.gather(
        *(storage.record_referral(referrer.id, referee.id) for _ in range(10))
    )

    assert sum(results) == 1


async def test_referrals_are_counted_since_moment(storage: Storage) -> None:
    """Нужно для суточного лимита наград на одного пригласившего."""
    referrer = await _make_user(storage, "1")
    referee = await _make_user(storage, "2")
    before = datetime.now(UTC) - timedelta(minutes=1)

    await storage.record_referral(referrer.id, referee.id)

    assert await storage.count_referrals_since(referrer.id, before) == 1
    assert (
        await storage.count_referrals_since(
            referrer.id, datetime.now(UTC) + timedelta(minutes=1)
        )
        == 0
    )


# --- Оплата --------------------------------------------------------------


async def test_a_created_payment_is_found_by_id(storage: Storage) -> None:
    user = await _make_user(storage, "pay-1")

    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    found = await storage.get_payment(order.id)

    assert found is not None
    assert found.user_id == user.id
    assert found.amount == 599
    assert found.status == "pending"


async def test_an_unknown_payment_is_none(storage: Storage) -> None:
    assert await storage.get_payment("нет такого заказа") is None


async def test_two_payments_never_share_an_id(storage: Storage) -> None:
    """Идентификатор служит ключом идемпотентности у провайдера."""
    user = await _make_user(storage, "pay-2")

    first = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    second = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )

    assert first.id != second.id


async def test_the_provider_id_is_remembered(storage: Storage) -> None:
    user = await _make_user(storage, "pay-3")
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=299,
        currency="RUB",
        docs_version="2026-08-31",
    )

    await storage.attach_external_id(order.id, "2d0a1b")

    found = await storage.get_payment(order.id)
    assert found is not None
    assert found.external_id == "2d0a1b"


async def test_a_payment_is_marked_paid_once(storage: Storage) -> None:
    """Второй раз — False. На этом держится защита от двойной выдачи."""
    user = await _make_user(storage, "pay-4")
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="stars",
        amount=524,
        currency="XTR",
        docs_version="2026-08-31",
    )

    assert await storage.mark_paid(order.id) is True
    assert await storage.mark_paid(order.id) is False

    found = await storage.get_payment(order.id)
    assert found is not None
    assert found.status == "paid"
    assert found.paid_at is not None


async def test_an_unknown_payment_cannot_be_marked_paid(storage: Storage) -> None:
    assert await storage.mark_paid("выдуманный заказ") is False


async def test_concurrent_confirmations_grant_only_once(storage: Storage) -> None:
    """Уведомления об оплате приходят пачкой и обрабатываются параллельно.

    Написан специально ради PostgreSQL: в памяти внутри операции нет ни одного
    await, и атомарность получается сама собой. В базе её обеспечивает условие
    на статус внутри самого UPDATE.
    """
    user = await _make_user(storage, "pay-5")
    order = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.MAX,
        method="card",
        amount=1490,
        currency="RUB",
        docs_version="2026-08-31",
    )

    results = await asyncio.gather(*(storage.mark_paid(order.id) for _ in range(10)))

    assert results.count(True) == 1, "подписка выдана бы несколько раз"


async def test_one_provider_payment_cannot_close_two_orders(storage: Storage) -> None:
    """Иначе одна оплата включала бы две подписки.

    Проверка на стороне хранилища, а не в коде сценария: «мы аккуратно
    проверили» — обещание, ограничение уникальности — гарантия.
    """
    user = await _make_user(storage, "pay-6")
    first = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    second = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )

    assert await storage.attach_external_id(first.id, "2d0a1b") is True
    assert await storage.attach_external_id(second.id, "2d0a1b") is False

    found = await storage.get_payment(second.id)
    assert found is not None
    assert found.external_id is None


# --- Подписка ------------------------------------------------------------


async def _make_subscription(
    storage: Storage,
    user: User,
    *,
    status: str = "active",
    next_charge_at: datetime | None = None,
    amount: int = 599,
    currency: str = "RUB",
) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        status=status,
        amount=amount,
        currency=currency,
        next_charge_at=next_charge_at or MOMENT,
        created_at=MOMENT,
        payment_method_id="card-1",
    )
    await storage.save_subscription(subscription)
    return subscription


async def test_a_saved_subscription_is_read_back(storage: Storage) -> None:
    user = await _make_user(storage, "sub-1")

    await _make_subscription(storage, user)
    found = await storage.get_subscription(user.id)

    assert found is not None
    assert found.tariff is TariffId.PRO
    assert found.amount == 599
    assert found.currency == "RUB"
    assert found.payment_method_id == "card-1"


async def test_a_user_without_a_subscription_has_none(storage: Storage) -> None:
    user = await _make_user(storage, "sub-2")

    assert await storage.get_subscription(user.id) is None


async def test_saving_twice_keeps_one_subscription(storage: Storage) -> None:
    """Две строки на человека означали бы два списания в месяц."""
    user = await _make_user(storage, "sub-3")

    await _make_subscription(storage, user)
    await _make_subscription(storage, user, amount=1490)

    found = await storage.get_subscription(user.id)
    assert found is not None
    assert found.amount == 1490


async def test_cancelling_stops_future_charges(storage: Storage) -> None:
    user = await _make_user(storage, "sub-4")
    await _make_subscription(storage, user)

    assert await storage.cancel_subscription(user.id, MOMENT) is True

    found = await storage.get_subscription(user.id)
    assert found is not None
    assert found.status == "cancelled"
    assert found.cancelled_at is not None


async def test_cancelling_twice_changes_nothing(storage: Storage) -> None:
    """Второе нажатие «отключить» не должно выглядеть как новая отмена."""
    user = await _make_user(storage, "sub-5")
    await _make_subscription(storage, user)

    assert await storage.cancel_subscription(user.id, MOMENT) is True
    assert await storage.cancel_subscription(user.id, MOMENT) is False


async def test_cancelling_a_missing_subscription_is_false(storage: Storage) -> None:
    user = await _make_user(storage, "sub-6")

    assert await storage.cancel_subscription(user.id, MOMENT) is False


async def test_only_due_subscriptions_are_charged(storage: Storage) -> None:
    due = await _make_user(storage, "sub-7")
    later = await _make_user(storage, "sub-8")
    await _make_subscription(storage, due, next_charge_at=MOMENT - timedelta(hours=1))
    await _make_subscription(storage, later, next_charge_at=MOMENT + timedelta(days=5))

    found = await storage.subscriptions_to_charge(MOMENT, limit=10)

    assert [each.user_id for each in found] == [due.id]


async def test_a_cancelled_subscription_is_never_charged(storage: Storage) -> None:
    """Оплаченный срок дорабатывает, но новых денег с человека не берут."""
    user = await _make_user(storage, "sub-9")
    await _make_subscription(
        storage, user, status="cancelled", next_charge_at=MOMENT - timedelta(days=1)
    )

    assert await storage.subscriptions_to_charge(MOMENT, limit=10) == []


async def test_reminders_go_out_once_per_charge(storage: Storage) -> None:
    """Пропустить обязательное предупреждение нельзя, повторить — раздражает."""
    user = await _make_user(storage, "sub-10")
    charge_at = MOMENT + timedelta(hours=12)
    await _make_subscription(storage, user, next_charge_at=charge_at)

    first = await storage.subscriptions_to_remind(
        MOMENT, MOMENT + timedelta(days=1), limit=10
    )
    await storage.mark_reminded(user.id, charge_at)
    second = await storage.subscriptions_to_remind(
        MOMENT, MOMENT + timedelta(days=1), limit=10
    )

    assert [each.user_id for each in first] == [user.id]
    assert second == []


async def test_a_new_charge_needs_a_new_reminder(storage: Storage) -> None:
    """Отметка привязана к дате списания, а не к самому факту напоминания."""
    user = await _make_user(storage, "sub-11")
    charge_at = MOMENT + timedelta(hours=12)
    await _make_subscription(storage, user, next_charge_at=charge_at)
    await storage.mark_reminded(user.id, charge_at)

    await _make_subscription(
        storage, user, next_charge_at=charge_at + timedelta(days=30)
    )
    due = await storage.subscriptions_to_remind(
        MOMENT, charge_at + timedelta(days=31), limit=10
    )

    assert [each.user_id for each in due] == [user.id]


async def test_the_price_is_checked_once_per_charge(storage: Storage) -> None:
    """Иначе сверка повторялась бы каждый проход всю неделю до списания."""
    user = await _make_user(storage, "sub-12")
    charge_at = MOMENT + timedelta(days=5)
    await _make_subscription(storage, user, next_charge_at=charge_at)

    first = await storage.subscriptions_to_check_price(
        MOMENT, MOMENT + timedelta(days=7), limit=10
    )
    await storage.mark_price_checked(user.id, charge_at)
    second = await storage.subscriptions_to_check_price(
        MOMENT, MOMENT + timedelta(days=7), limit=10
    )

    assert [each.user_id for each in first] == [user.id]
    assert second == []


async def test_an_overdue_charge_gets_no_tomorrow_reminder(storage: Storage) -> None:
    """У просроченного списания «завтра» уже прошло — предупреждать поздно.

    Такими занимается проход списаний: он переносит срок и предупреждает
    заново. Попади они сюда, человек получил бы письмо про завтрашние деньги
    в тот же час, когда их снимут.
    """
    user = await _make_user(storage, "sub-13")
    await _make_subscription(storage, user, next_charge_at=MOMENT - timedelta(hours=1))

    due = await storage.subscriptions_to_remind(
        MOMENT, MOMENT + timedelta(days=1), limit=10
    )

    assert due == []


async def test_advancing_moves_the_next_charge(storage: Storage) -> None:
    user = await _make_user(storage, "sub-14")
    await _make_subscription(storage, user)
    later = MOMENT + timedelta(days=30)

    moved = await storage.advance_subscription(
        user.id, next_charge_at=later, status="past_due", failed_since=MOMENT
    )

    found = await storage.get_subscription(user.id)
    assert moved is True
    assert found is not None
    assert found.next_charge_at == later
    assert found.status == "past_due"
    assert found.failed_since == MOMENT
    assert found.amount == 599


async def test_advancing_can_change_the_price(storage: Storage) -> None:
    user = await _make_user(storage, "sub-15")
    await _make_subscription(storage, user)

    await storage.advance_subscription(
        user.id,
        next_charge_at=MOMENT,
        status="active",
        failed_since=None,
        amount=699,
    )

    found = await storage.get_subscription(user.id)
    assert found is not None
    assert found.amount == 699


async def test_a_cancelled_subscription_is_never_advanced(storage: Storage) -> None:
    """Ради этого условия метод и существует.

    Фоновый проход держит копию, прочитанную в его начале, и между чтением и
    записью помещается нажатие «Отключить продление». Записать копию целиком
    значило бы воскресить отменённую подписку и списать деньги в следующем
    месяце.
    """
    user = await _make_user(storage, "sub-16")
    await _make_subscription(storage, user)
    await storage.cancel_subscription(user.id, MOMENT)

    moved = await storage.advance_subscription(
        user.id,
        next_charge_at=MOMENT + timedelta(days=30),
        status="active",
        failed_since=None,
    )

    found = await storage.get_subscription(user.id)
    assert moved is False
    assert found is not None
    assert found.status == "cancelled"
    assert found.next_charge_at == MOMENT


async def test_advancing_a_missing_subscription_is_false(storage: Storage) -> None:
    user = await _make_user(storage, "sub-17")

    assert (
        await storage.advance_subscription(
            user.id, next_charge_at=MOMENT, status="active", failed_since=None
        )
        is False
    )


# --- Имя пользователя ----------------------------------------------------


async def test_a_user_without_a_name_is_marked_so(storage: Storage) -> None:
    """Пустая ячейка не отличает «имени нет» от «мы его не записали»."""
    user = await _make_user(storage, "name-1")

    assert user.username == NO_USERNAME
    found = await storage.get_user(MessengerKind.TELEGRAM, "name-1")
    assert found is not None
    assert found.username == NO_USERNAME


async def test_the_name_is_kept_from_the_start(storage: Storage) -> None:
    user = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="name-2",
        referral_code="codename2",
        support_number=support.generate_number(),
        daily_image_quota=3,
        username="durov",
    )

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.username == "durov"


async def test_the_name_can_be_refreshed(storage: Storage) -> None:
    """Имя меняют когда захотят: записанное однажды через месяц уже чужое."""
    user = await _make_user(storage, "name-3")

    await storage.set_username(user.id, "newname")

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.username == "newname"


async def test_losing_the_name_is_recorded_too(storage: Storage) -> None:
    """Человек снял себе имя — в базе это должно быть видно, а не забыто."""
    user = await _make_user(storage, "name-4")
    await storage.set_username(user.id, "hadaname")

    await storage.set_username(user.id, NO_USERNAME)

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.username == NO_USERNAME
