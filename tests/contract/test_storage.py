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
from app.core.models import (
    ChatTurn,
    DialogState,
    MessengerKind,
    Role,
    TariffId,
    User,
)
from app.ports.storage import Storage

DAY = date(2026, 8, 28)
NEXT_DAY = date(2026, 8, 29)


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
            text("TRUNCATE referrals, dialogs, usage, users RESTART IDENTITY CASCADE")
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
        daily_image_quota=3,
    )

    assert telegram_user.id != max_user.id


async def test_duplicate_external_id_is_rejected(storage: Storage) -> None:
    await _make_user(storage, "5")

    with pytest.raises(ValueError):
        await _make_user(storage, "5")


async def test_duplicate_referral_code_is_rejected(storage: Storage) -> None:
    await _make_user(storage, "1")

    with pytest.raises(ValueError):
        await storage.create_user(
            messenger=MessengerKind.TELEGRAM,
            external_id="2",
            referral_code="code1",
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
    )
    second = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
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
    )
    second = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
    )

    assert await storage.attach_external_id(first.id, "2d0a1b") is True
    assert await storage.attach_external_id(second.id, "2d0a1b") is False

    found = await storage.get_payment(second.id)
    assert found is not None
    assert found.external_id is None
