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
from app.core.generations import Generation, GenerationKind, GenerationStatus
from app.core.limits import LimitKind
from app.core.models import (
    NO_USERNAME,
    ChatTurn,
    DialogState,
    MessengerKind,
    Payment,
    Role,
    Subscription,
    TariffId,
    User,
)
from app.ports.payments import PaymentStatus
from app.ports.storage import GrantOutcome, Storage

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


async def _make_user(
    storage: Storage, external_id: str = "1", *, bonus_images: int = 0
) -> User:
    """Пользователь без подарка при регистрации, если его не попросили.

    Ноль по умолчанию не ради краткости: почти каждая проверка ниже считает
    бонусы, и молчаливая тройка в них превращала бы арифметику теста в
    загадку. Саму выдачу проверяет отдельный тест.
    """
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id=external_id,
        referral_code=f"code{external_id}",
        support_number=support.generate_number(),
        bonus_images=bonus_images,
        bonus_documents=0,
    )


async def _payment(storage: Storage, user: User) -> Payment:
    """Заказ на подписку — общая заготовка для проверок про деньги."""
    return await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )


# --- Пользователи --------------------------------------------------------


async def test_created_user_is_found_by_external_id(storage: Storage) -> None:
    created = await _make_user(storage, "42", bonus_images=3)

    found = await storage.get_user(MessengerKind.TELEGRAM, "42")

    assert found is not None
    assert found.id == created.id
    assert found.tariff is TariffId.FREE
    assert found.bonus_messages == 0
    # Картинки при регистрации кладутся в бонус той же вставкой.
    assert found.bonus_images == 3
    assert found.channel_checked_at is None
    assert found.stopped_at is None


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
        bonus_images=3,
        bonus_documents=0,
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
        bonus_images=3,
        bonus_documents=0,
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
                bonus_images=3,
                bonus_documents=0,
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
            bonus_images=3,
            bonus_documents=0,
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
    assert usage.day == DAY


async def test_usage_accumulates(storage: Storage) -> None:
    user = await _make_user(storage)

    await storage.add_usage(user.id, DAY, messages=1)
    await storage.add_usage(user.id, DAY, messages=1)

    usage = await storage.get_usage(user.id, DAY)
    assert usage.messages_used == 2


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


async def test_the_document_bonus_is_added_and_spent(storage: Storage) -> None:
    """Третья корзина живёт отдельно от двух прежних."""
    user = await _make_user(storage)

    await storage.add_bonus(user.id, documents=3)
    assert await storage.spend_bonus(user.id, documents=1) is True

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_documents == 2


async def test_spending_documents_does_not_touch_the_other_baskets(
    storage: Storage,
) -> None:
    """Иначе разбор файла молча съедал бы картинки, оплаченные отдельно."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, messages=7, images=5, documents=3)

    assert await storage.spend_bonus(user.id, documents=2) is True

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert (updated.bonus_messages, updated.bonus_images) == (7, 5)
    assert updated.bonus_documents == 1


async def test_documents_cannot_be_overspent(storage: Storage) -> None:
    """Всё-или-ничего и здесь: остаток не уходит в минус."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, documents=1)

    assert await storage.spend_bonus(user.id, documents=2) is False

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_documents == 1


async def test_a_short_document_basket_blocks_the_whole_spend(
    storage: Storage,
) -> None:
    """Списание сразу из двух корзин не должно пройти наполовину."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, images=5, documents=0)

    assert await storage.spend_bonus(user.id, images=1, documents=1) is False

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.bonus_images == 5


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


# --- Отмена прежней звёздной подписки (фаза 11, часть 3) ----------------


async def test_a_star_cancel_waits_in_the_queue_until_done(storage: Storage) -> None:
    user = await _make_user(storage)

    await storage.queue_star_cancel(user.id, "charge-1", MOMENT)
    await storage.queue_star_cancel(user.id, "charge-1", MOMENT)

    due = await storage.star_cancels_due(limit=10)
    assert [(c.user_id, c.charge_id, c.queued_at) for c in due] == [
        (user.id, "charge-1", MOMENT)
    ]
    await storage.star_cancel_done("charge-1")
    assert await storage.star_cancels_due(limit=10) == []


async def test_star_cancels_come_oldest_first_and_up_to_the_limit(
    storage: Storage,
) -> None:
    user = await _make_user(storage)
    await storage.queue_star_cancel(user.id, "late", MOMENT + timedelta(hours=1))
    await storage.queue_star_cancel(user.id, "early", MOMENT)

    due = await storage.star_cancels_due(limit=1)

    assert [c.charge_id for c in due] == ["early"]


# --- Месячная норма (фаза 11) --------------------------------------------

PERIOD = MOMENT
NEXT_PERIOD = MOMENT + timedelta(days=30)


async def test_period_usage_starts_at_zero(storage: Storage) -> None:
    user = await _make_user(storage)

    usage = await storage.get_period_usage(user.id, PERIOD)

    assert (usage.images_used, usage.documents_used, usage.presentations_used) == (
        0,
        0,
        0,
    )


async def test_the_norm_is_spent_up_to_its_limit(storage: Storage) -> None:
    user = await _make_user(storage)

    spent = [
        await storage.spend_norm(user.id, PERIOD, LimitKind.IMAGES, limit=2)
        for _ in range(3)
    ]

    assert spent == [True, True, False]
    assert (await storage.get_period_usage(user.id, PERIOD)).images_used == 2


async def test_the_norm_is_counted_per_period_kind_and_person(
    storage: Storage,
) -> None:
    """Новый период начинается с нуля; виды и люди друг другу не мешают."""
    user = await _make_user(storage, "1")
    other = await _make_user(storage, "2")

    await storage.spend_norm(user.id, PERIOD, LimitKind.DOCUMENTS, limit=5)
    await storage.spend_norm(user.id, PERIOD, LimitKind.PRESENTATIONS, limit=5)

    this = await storage.get_period_usage(user.id, PERIOD)
    assert (this.images_used, this.documents_used, this.presentations_used) == (
        0,
        1,
        1,
    )
    assert (await storage.get_period_usage(user.id, NEXT_PERIOD)).documents_used == 0
    assert (await storage.get_period_usage(other.id, PERIOD)).documents_used == 0


async def test_a_zero_norm_gives_nothing(storage: Storage) -> None:
    user = await _make_user(storage)

    assert not await storage.spend_norm(user.id, PERIOD, LimitKind.IMAGES, limit=0)
    assert (await storage.get_period_usage(user.id, PERIOD)).images_used == 0


async def test_the_norm_cannot_be_overspent_concurrently(storage: Storage) -> None:
    """Т3: десять одновременных списаний при норме 3 — ровно три успеха.

    Проверка остатка и списание — одна операция в базе: иначе одновременные
    запросы одного человека оба увидели бы «осталось одно» и оба списали.
    """
    user = await _make_user(storage)

    results = await asyncio.gather(
        *(
            storage.spend_norm(user.id, PERIOD, LimitKind.IMAGES, limit=3)
            for _ in range(10)
        )
    )

    assert sum(results) == 3
    assert (await storage.get_period_usage(user.id, PERIOD)).images_used == 3


async def test_a_grant_records_when_the_paid_norm_starts(storage: Storage) -> None:
    """Норма обновляется с каждой оплатой: начало периода пишется вместе с ней."""
    user = await _make_user(storage)
    order = await _payment(storage, user)

    await storage.complete_payment(
        order.id,
        tariff=TariffId.PRO,
        expires_at=MOMENT + timedelta(days=30),
        seen_tariff=user.tariff,
        seen_expiry=user.tariff_expires_at,
        subscription=None,
        norm_since=MOMENT,
    )

    found = await storage.get_user_by_id(user.id)
    assert found is not None and found.norm_since == MOMENT


# --- Бонус за подписку на канал ------------------------------------------


async def test_presentations_are_given_at_signup(storage: Storage) -> None:
    """К6: разовая презентация кладётся одной вставкой с пользователем."""
    user = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="p1",
        referral_code="codep1",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
        bonus_presentations=1,
    )

    assert user.bonus_presentations == 1
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.bonus_presentations == 1


async def test_presentations_are_added_and_spent_all_or_nothing(
    storage: Storage,
) -> None:
    user = await _make_user(storage)
    await storage.add_bonus(user.id, presentations=1)

    assert await storage.spend_bonus(user.id, presentations=1)
    assert not await storage.spend_bonus(user.id, presentations=1)
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.bonus_presentations == 0


async def test_a_presentation_cannot_be_spent_twice_concurrently(
    storage: Storage,
) -> None:
    """Одна презентация, два одновременных списания — успешно ровно одно."""
    user = await _make_user(storage)
    await storage.add_bonus(user.id, presentations=1)

    results = await asyncio.gather(
        *(storage.spend_bonus(user.id, presentations=1) for _ in range(5))
    )

    assert results.count(True) == 1


async def test_one_presentation_build_at_a_time(storage: Storage) -> None:
    """К5: слот сборки у человека один, и освобождается явно."""
    user = await _make_user(storage)
    window = timedelta(minutes=10)

    assert await storage.claim_presentation(user.id, MOMENT, stale_after=window)
    assert not await storage.claim_presentation(user.id, MOMENT, stale_after=window)

    await storage.release_presentation(user.id)
    assert await storage.claim_presentation(user.id, MOMENT, stale_after=window)


async def test_two_presses_at_once_claim_one_slot(storage: Storage) -> None:
    """К5 на настоящей базе: из одновременных захватов выигрывает один."""
    user = await _make_user(storage)

    results = await asyncio.gather(
        *(
            storage.claim_presentation(
                user.id, MOMENT, stale_after=timedelta(minutes=10)
            )
            for _ in range(5)
        )
    )

    assert results.count(True) == 1


async def test_an_abandoned_build_can_be_claimed_again(storage: Storage) -> None:
    """Сборку оборвала выкатка — слот не заперт навсегда."""
    user = await _make_user(storage)
    window = timedelta(minutes=10)
    assert await storage.claim_presentation(user.id, MOMENT, stale_after=window)

    later = MOMENT + timedelta(minutes=11)
    assert await storage.claim_presentation(user.id, later, stale_after=window)


async def test_a_channel_check_is_remembered(storage: Storage) -> None:
    """Сессия 8: подтверждение подписки помнится — Telegram не спрашиваем."""
    user = await _make_user(storage)
    assert user.channel_checked_at is None

    await storage.remember_channel_check(user.id, MOMENT)

    updated = await storage.get_user_by_id(user.id)
    assert updated is not None
    assert updated.channel_checked_at == MOMENT


async def test_the_stopped_mark_keeps_its_first_moment(storage: Storage) -> None:
    """Сессия 8: отметка «остановил бота» — когда это случилось впервые.

    Повторные отказы доставки не двигают её вперёд; любое действие человека
    снимает её целиком.
    """
    user = await _make_user(storage)
    assert user.stopped_at is None

    await storage.mark_stopped(user.id, MOMENT)
    await storage.mark_stopped(user.id, MOMENT + timedelta(hours=1))
    marked = await storage.get_user_by_id(user.id)
    assert marked is not None
    assert marked.stopped_at == MOMENT

    await storage.clear_stopped(user.id)
    cleared = await storage.get_user_by_id(user.id)
    assert cleared is not None
    assert cleared.stopped_at is None


async def test_a_generation_is_written_as_it_was_given(storage: Storage) -> None:
    user = await _make_user(storage)

    await storage.record_generation(
        Generation(
            user_id=user.id,
            kind=GenerationKind.PRESET,
            model="gpt-image-1",
            status=GenerationStatus.SUCCESS,
            duration_ms=1500,
            preset_id="lego",
            tokens_in=11,
            tokens_out=22,
        )
    )

    # Читать учёт продукту незачем, поэтому в порту метода чтения нет:
    # проверяем тем, что запись вообще прошла и не упала на ограничениях.


async def test_a_failed_generation_is_written_too(storage: Storage) -> None:
    """Провайдер берёт деньги за попытку, а не за успех."""
    user = await _make_user(storage)

    await storage.record_generation(
        Generation(
            user_id=user.id,
            kind=GenerationKind.CHAT,
            model="gpt-6-luna",
            status=GenerationStatus.FAILED,
            duration_ms=0,
            error_code="TimeoutError",
        )
    )


async def test_generations_of_one_user_do_not_collide(storage: Storage) -> None:
    """Строк на человека много: это журнал, а не одна запись на пользователя."""
    user = await _make_user(storage)
    one = Generation(
        user_id=user.id,
        kind=GenerationKind.IMAGE,
        model="gpt-image-1",
        status=GenerationStatus.SUCCESS,
        duration_ms=10,
    )

    await storage.record_generation(one)
    await storage.record_generation(one)


# --- Возврат денег -------------------------------------------------------


async def test_an_order_is_found_by_the_provider_payment_id(
    storage: Storage,
) -> None:
    """У возврата нашего идентификатора нет — только идентификатор платежа."""
    user = await _make_user(storage)
    order = await _payment(storage, user)
    await storage.attach_external_id(order.id, "yk-42")

    found = await storage.get_payment_by_external_id("yk-42")

    assert found is not None
    assert found.id == order.id


async def test_an_unknown_payment_id_finds_nothing(storage: Storage) -> None:
    assert await storage.get_payment_by_external_id("yk-нет-такого") is None


async def test_a_paid_order_becomes_refunded_once(storage: Storage) -> None:
    """Уведомлений о возврате приходит несколько, возврат при этом один."""
    user = await _make_user(storage)
    order = await _payment(storage, user)
    assert await storage.mark_paid(order.id)

    assert await storage.mark_refunded(order.id) is True
    assert await storage.mark_refunded(order.id) is False

    found = await storage.get_payment(order.id)
    assert found is not None
    assert found.status == PaymentStatus.REFUNDED.value


async def test_an_unpaid_order_cannot_be_refunded(storage: Storage) -> None:
    """Вернуть можно то, что получили.

    Уведомление о возврате по неоплаченному заказу означает, что мы чего-то
    не знаем, и молча переписывать статус в такой ситуации нельзя.
    """
    user = await _make_user(storage)
    order = await _payment(storage, user)

    assert await storage.mark_refunded(order.id) is False

    found = await storage.get_payment(order.id)
    assert found is not None
    assert found.status == PaymentStatus.PENDING.value


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
    remind_before_charge: bool = True,
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
        remind_before_charge=remind_before_charge,
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
    # Сохранённую карту у ЮKassa не удалить: платежи по ней проходят, пока мы
    # их создаём, и отключение автоплатежа целиком на нашей стороне. Поэтому
    # отмена забывает идентификатор, а не только меняет статус — второй слой
    # к тому же запрету, как и везде, где дело касается денег.
    assert found.payment_method_id is None


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


async def test_an_ordinary_renewal_gets_no_reminder(storage: Storage) -> None:
    """0г: напоминание — только перед списанием, которому оно положено.

    Обычные продления идут без «завтра спишем» (решение заказчика); иначе
    проход напоминаний выбирал бы их каждый тик и упирался в предел выборки.
    """
    user = await _make_user(storage, "sub-10b")
    charge_at = MOMENT + timedelta(hours=12)
    await _make_subscription(
        storage, user, next_charge_at=charge_at, remind_before_charge=False
    )

    due = await storage.subscriptions_to_remind(
        MOMENT, MOMENT + timedelta(days=1), limit=10
    )

    assert due == []
    found = await storage.get_subscription(user.id)
    assert found is not None and found.remind_before_charge is False


async def test_the_reminder_mark_is_read_back(storage: Storage) -> None:
    user = await _make_user(storage, "sub-10c")
    await _make_subscription(storage, user, remind_before_charge=True)

    found = await storage.get_subscription(user.id)

    assert found is not None and found.remind_before_charge is True


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


async def test_a_charge_order_is_held_until_released(storage: Storage) -> None:
    """Один период — один заказ: пока исход неизвестен, второй не занять."""
    user = await _make_user(storage, "sub-hold-1")
    await _make_subscription(storage, user)
    first = await _payment(storage, user)
    second = await _payment(storage, user)

    assert await storage.hold_charge_order(user.id, first.id) is True
    assert await storage.hold_charge_order(user.id, second.id) is False
    found = await storage.get_subscription(user.id)
    assert found is not None and found.charge_order_id == first.id

    # Отпустить можно только тот заказ, который держишь: чужой отпуск —
    # это опоздавший проход, и он не должен освобождать место новому заказу.
    await storage.release_charge_order(user.id, second.id)
    found = await storage.get_subscription(user.id)
    assert found is not None and found.charge_order_id == first.id

    await storage.release_charge_order(user.id, first.id)
    assert await storage.hold_charge_order(user.id, second.id) is True


async def test_a_cancelled_subscription_holds_no_charge_order(
    storage: Storage,
) -> None:
    user = await _make_user(storage, "sub-hold-2")
    await _make_subscription(storage, user)
    await storage.cancel_subscription(user.id, MOMENT)
    order = await _payment(storage, user)

    assert await storage.hold_charge_order(user.id, order.id) is False


async def test_the_grant_releases_the_held_order(storage: Storage) -> None:
    """Выдача периода закрывает и его заказ: следующий период — новый заказ."""
    user = await _make_user(storage, "sub-hold-3")
    await _make_subscription(storage, user)
    order = await _payment(storage, user)
    await storage.hold_charge_order(user.id, order.id)

    await storage.complete_payment(
        order.id,
        tariff=TariffId.PRO,
        expires_at=MOMENT + timedelta(days=30),
        seen_tariff=user.tariff,
        seen_expiry=user.tariff_expires_at,
        norm_since=MOMENT,
        subscription=_new_subscription(user),
    )

    found = await storage.get_subscription(user.id)
    assert found is not None and found.charge_order_id is None


async def test_retrying_the_same_charge_needs_no_new_reminder(
    storage: Storage,
) -> None:
    """Повтор того же списания — не новое списание: человек уже предупреждён.

    Без этого перенос срока на час выглядел бы для прохода напоминаний как
    новое списание, и человек получал бы «завтра спишем» каждый час.
    """
    user = await _make_user(storage, "sub-hold-4")
    await _make_subscription(storage, user)
    await storage.mark_reminded(user.id, MOMENT)
    later = MOMENT + timedelta(hours=1)

    await storage.advance_subscription(
        user.id,
        next_charge_at=later,
        status="active",
        failed_since=None,
        same_charge=True,
    )

    found = await storage.get_subscription(user.id)
    assert found is not None
    assert (found.reminded_for, found.price_checked_for) == (later, later)
    assert (
        await storage.subscriptions_to_remind(
            MOMENT, MOMENT + timedelta(days=1), limit=10
        )
        == []
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
        bonus_images=3,
        bonus_documents=0,
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


# --- Почта для чека ------------------------------------------------------


async def test_a_new_user_has_no_email(storage: Storage) -> None:
    """Пусто означает ровно «картой не платил», а не «мы не записали»."""
    user = await _make_user(storage, "mail-1")

    assert user.email is None


async def test_the_email_survives_a_reread(storage: Storage) -> None:
    """По ней уходят чеки автосписаний — там спросить будет уже не у кого."""
    user = await _make_user(storage, "mail-2")

    await storage.set_email(user.id, "alika@mail.ru")

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.email == "alika@mail.ru"


async def test_a_long_address_still_fits(storage: Storage) -> None:
    """254 символа — предел адреса по RFC 5321, и колонка обязана его вмещать."""
    user = await _make_user(storage, "mail-3")
    longest = "a" * (254 - len("@mail.ru")) + "@mail.ru"

    await storage.set_email(user.id, longest)

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.email == longest


# --- Атомарная выдача (фаза 11, П4) --------------------------------------


def _new_subscription(user: User, *, amount: int = 599) -> Subscription:
    return Subscription(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="card",
        status="active",
        amount=amount,
        currency="RUB",
        next_charge_at=MOMENT + timedelta(days=30),
        created_at=MOMENT,
        payment_method_id="card-1",
    )


# --- Пробный период (фаза 11, часть 3) -----------------------------------


async def _trial_order(storage: Storage, user: User) -> Payment:
    return await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.LITE,
        method="card",
        amount=1,
        currency="RUB",
        docs_version="2026-10-03",
        trial=True,
    )


async def _grant_trial(storage: Storage, user: User, order: Payment) -> GrantOutcome:
    return await storage.complete_payment(
        order.id,
        tariff=TariffId.LITE,
        expires_at=MOMENT + timedelta(days=3),
        seen_tariff=user.tariff,
        seen_expiry=user.tariff_expires_at,
        subscription=None,
        norm_since=MOMENT,
        trial=True,
    )


async def test_a_trial_order_is_read_back_as_one(storage: Storage) -> None:
    user = await _make_user(storage, "trial-1")

    order = await _trial_order(storage, user)

    found = await storage.get_payment(order.id)
    assert found is not None and found.trial is True
    plain = await storage.get_payment((await _payment(storage, user)).id)
    assert plain is not None and plain.trial is False


async def test_the_trial_is_granted_once_and_remembered(storage: Storage) -> None:
    user = await _make_user(storage, "trial-2")
    order = await _trial_order(storage, user)

    assert await _grant_trial(storage, user, order) is GrantOutcome.GRANTED

    found = await storage.get_user_by_id(user.id)
    assert found is not None
    assert found.trial_order_id == order.id
    assert found.tariff_expires_at == MOMENT + timedelta(days=3)


async def test_a_second_trial_payment_is_paid_but_grants_nothing(
    storage: Storage,
) -> None:
    """ПП3: вторая оплата 1 ₽ — деньги взяты, заказ оплачен, второго нет."""
    user = await _make_user(storage, "trial-3")
    first = await _trial_order(storage, user)
    second = await _trial_order(storage, user)
    await _grant_trial(storage, user, first)
    granted = await storage.get_user_by_id(user.id)
    assert granted is not None

    outcome = await _grant_trial(storage, granted, second)

    assert outcome is GrantOutcome.TRIAL_USED
    paid = await storage.get_payment(second.id)
    assert paid is not None and paid.status == "paid"
    found = await storage.get_user_by_id(user.id)
    assert found is not None and found.trial_order_id == first.id


async def test_someone_who_paid_before_gets_no_trial(storage: Storage) -> None:
    user = await _make_user(storage, "trial-4")
    earlier = await _payment(storage, user)
    assert await storage.mark_paid(earlier.id)
    order = await _trial_order(storage, user)

    assert await _grant_trial(storage, user, order) is GrantOutcome.TRIAL_USED
    found = await storage.get_user_by_id(user.id)
    assert found is not None and found.trial_order_id is None


async def test_two_trial_payments_at_once_grant_one(storage: Storage) -> None:
    """ПП3 на базе: два одновременных подтверждения — один пробный период."""
    user = await _make_user(storage, "trial-5")
    first = await _trial_order(storage, user)
    second = await _trial_order(storage, user)

    outcomes = list(
        await asyncio.gather(
            _grant_trial(storage, user, first), _grant_trial(storage, user, second)
        )
    )
    # Второе может упереться в строку, изменённую первым, — тогда STALE, и
    # вызывающий пересчитывает, как это делает payments.confirm.
    for index, (order, outcome) in enumerate(
        zip((first, second), outcomes, strict=True)
    ):
        if outcome is GrantOutcome.STALE:
            fresh = await storage.get_user_by_id(user.id)
            assert fresh is not None
            outcomes[index] = await _grant_trial(storage, fresh, order)

    assert sorted(outcome.value for outcome in outcomes) == sorted(
        [GrantOutcome.GRANTED.value, GrantOutcome.TRIAL_USED.value]
    )
    found = await storage.get_user_by_id(user.id)
    assert found is not None and found.trial_order_id in {first.id, second.id}


async def test_ever_paid_counts_paid_and_refunded_orders(storage: Storage) -> None:
    user = await _make_user(storage, "trial-6")
    assert await storage.ever_paid(user.id) is False
    pending = await _payment(storage, user)
    assert await storage.ever_paid(user.id) is False

    assert await storage.mark_paid(pending.id)
    assert await storage.ever_paid(user.id) is True
    assert await storage.mark_refunded(pending.id)
    assert await storage.ever_paid(user.id) is True


async def test_completing_a_payment_grants_once(storage: Storage) -> None:
    """П4: заказ, тариф и подписка — одним шагом и ровно один раз."""
    user = await _make_user(storage, "grant-1")
    order = await _payment(storage, user)
    until = MOMENT + timedelta(days=30)

    first = await storage.complete_payment(
        order.id,
        tariff=TariffId.PRO,
        expires_at=until,
        seen_tariff=user.tariff,
        seen_expiry=user.tariff_expires_at,
        norm_since=MOMENT,
        subscription=_new_subscription(user),
    )
    second = await storage.complete_payment(
        order.id,
        tariff=TariffId.PRO,
        expires_at=until + timedelta(days=30),
        seen_tariff=TariffId.PRO,
        seen_expiry=until,
        norm_since=MOMENT,
        subscription=None,
    )

    assert (first, second) == (GrantOutcome.GRANTED, GrantOutcome.ALREADY)
    paid = await storage.get_payment(order.id)
    assert paid is not None and paid.status == PaymentStatus.PAID.value
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    assert (fresh.tariff, fresh.tariff_expires_at) == (TariffId.PRO, until)
    subscription = await storage.get_subscription(user.id)
    assert subscription is not None and subscription.next_charge_at == until


async def test_two_notices_at_once_grant_once(storage: Storage) -> None:
    """П4: два одновременных подтверждения — одна выдача, один срок."""
    user = await _make_user(storage, "grant-2")
    order = await _payment(storage, user)
    until = MOMENT + timedelta(days=30)

    outcomes = await asyncio.gather(
        *(
            storage.complete_payment(
                order.id,
                tariff=TariffId.PRO,
                expires_at=until,
                seen_tariff=user.tariff,
                seen_expiry=user.tariff_expires_at,
                norm_since=MOMENT,
                subscription=None,
            )
            for _ in range(5)
        )
    )

    assert outcomes.count(GrantOutcome.GRANTED) == 1


async def test_a_stale_view_of_the_tariff_grants_nothing(storage: Storage) -> None:
    """Срок поменялся, пока считали новый, — заказ остаётся ждать пересчёта.

    Иначе два разных заказа одного человека, оплаченные почти одновременно,
    продлили бы срок от одной и той же старой даты, и месяц потерялся бы.
    """
    user = await _make_user(storage, "grant-3")
    order = await _payment(storage, user)

    outcome = await storage.complete_payment(
        order.id,
        tariff=TariffId.PRO,
        expires_at=MOMENT + timedelta(days=30),
        seen_tariff=TariffId.PRO,
        seen_expiry=MOMENT,
        norm_since=MOMENT,
        subscription=None,
    )

    assert outcome is GrantOutcome.STALE
    pending = await storage.get_payment(order.id)
    assert pending is not None and pending.status == PaymentStatus.PENDING.value
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.tariff is TariffId.FREE


# --- Сверка зависших заказов (фаза 11, П5) -------------------------------


async def test_reconciliation_picks_pending_card_orders_of_the_right_age(
    storage: Storage,
) -> None:
    """Только pending, только с платежом у провайдера, только картой и в окне."""
    user = await _make_user(storage, "rec-1")
    due = await _payment(storage, user)
    assert await storage.attach_external_id(due.id, "ext-due")
    no_external = await _payment(storage, user)
    paid = await _payment(storage, user)
    assert await storage.attach_external_id(paid.id, "ext-paid")
    assert await storage.mark_paid(paid.id)
    stars = await storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method="stars",
        amount=524,
        currency="XTR",
        docs_version="2026-08-31",
    )
    assert await storage.attach_external_id(stars.id, "charge-stars")

    now = datetime.now(UTC)
    found = await storage.payments_to_reconcile(
        created_before=now + timedelta(minutes=1),
        created_after=now - timedelta(days=3),
        limit=10,
    )
    too_old = await storage.payments_to_reconcile(
        created_before=now - timedelta(days=4),
        created_after=now - timedelta(days=5),
        limit=10,
    )

    assert [order.id for order in found] == [due.id]
    assert no_external.id not in [order.id for order in found]
    assert too_old == []
