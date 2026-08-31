"""Хранилище на PostgreSQL.

Реализует тот же порт, что и InMemoryStorage, и проходит тот же набор
контрактных тестов — включая параллельные. Именно параллельные тесты здесь
главные: в памяти атомарность получалась сама собой (внутри операции нет ни
одного await), а тут её обеспечивают ограничения и блокировки строк, и это
надо доказывать.

Каждая операция выполняется в собственной транзакции. Долгих транзакций,
удерживаемых на время похода к провайдеру ИИ, здесь нет и быть не должно:
картинка рисуется пятнадцать секунд, и держать всё это время блокировку на
строке пользователя значит выстроить очередь на ровном месте.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.adapters.storage.schema import (
    dialogs,
    payments,
    referrals,
    usage,
    users,
)
from app.core.models import (
    ChatTurn,
    DialogState,
    MessengerKind,
    Payment,
    Role,
    TariffId,
    Usage,
    User,
    UserId,
)
from app.ports.payments import PaymentStatus


def create_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    """Создаёт движок для asyncpg.

    ``pool_pre_ping`` не роскошь: управляемый PostgreSQL закрывает простаивающие
    соединения, и без проверки первый же запрос после затишья падал бы у
    живого пользователя.
    """
    return create_async_engine(
        normalise_dsn(dsn),
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        # Без этого текст ошибки SQLAlchemy содержит запрос вместе с
        # параметрами, а среди параметров — переписка пользователя. В логах
        # содержимого сообщений быть не должно (§3.5), а ошибка базы рано или
        # поздно окажется в логе.
        hide_parameters=True,
    )


def normalise_dsn(dsn: str) -> str:
    """Приводит строку подключения к драйверу asyncpg.

    Railway отдаёт DATABASE_URL в виде postgresql://…, а SQLAlchemy нужен
    явный драйвер. Чинить это руками в переменной окружения при каждом
    пересоздании базы — лишний повод для ошибки.
    """
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if dsn.startswith(prefix):
            return dsn
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn.removeprefix(prefix)
    return dsn


class PostgresStorage:
    """Реализация порта Storage поверх PostgreSQL."""

    def __init__(
        self,
        engine: AsyncEngine,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._now = clock if clock is not None else _utc_now

    # --- Пользователи --------------------------------------------------

    async def get_user(self, messenger: MessengerKind, external_id: str) -> User | None:
        query = select(users).where(
            users.c.messenger == messenger.value,
            users.c.external_id == external_id,
        )
        return await self._fetch_user(query)

    async def get_user_by_id(self, user_id: UserId) -> User | None:
        return await self._fetch_user(select(users).where(users.c.id == user_id))

    async def get_user_by_referral_code(self, code: str) -> User | None:
        return await self._fetch_user(
            select(users).where(users.c.referral_code == code)
        )

    async def create_user(
        self,
        *,
        messenger: MessengerKind,
        external_id: str,
        referral_code: str,
        daily_image_quota: int,
        referred_by: UserId | None = None,
    ) -> User:
        query = (
            insert(users)
            .values(
                messenger=messenger.value,
                external_id=external_id,
                tariff=TariffId.FREE.value,
                referral_code=referral_code,
                created_at=self._now(),
                daily_image_quota=daily_image_quota,
                referred_by=referred_by,
            )
            # Гонка за нового человека разрешается базой, а не проверкой в
            # коде: два первых обновления обрабатываются параллельно, и оба
            # видят «его ещё нет». Побеждает один, второй ничего не пишет.
            .on_conflict_do_nothing(
                index_elements=[users.c.messenger, users.c.external_id]
            )
            .returning(users)
        )
        async with self._session() as session, session.begin():
            try:
                row = (await session.execute(query)).mappings().one_or_none()
            except IntegrityError as error:
                # Сюда попадаем только на занятом реферальном коде: конфликт
                # по паре (messenger, external_id) выше разрешён молчанием.
                # Порт обещает ValueError, а не диалектное исключение: ядро не
                # должно знать, что под ним PostgreSQL.
                raise ValueError(
                    f"реферальный код {referral_code} уже занят"
                ) from error

        if row is not None:
            return _to_user(row)

        # Ничего не вставилось — значит человек уже заведён параллельно.
        found = await self.get_user(messenger, external_id)
        if found is None:
            raise ValueError(
                f"пользователь {messenger.value}:{external_id} не найден "
                "после конфликта вставки"
            )
        return found

    async def set_tariff(
        self,
        user_id: UserId,
        tariff: TariffId,
        expires_at: datetime | None,
    ) -> None:
        query = (
            update(users)
            .where(users.c.id == user_id)
            .values(tariff=tariff.value, tariff_expires_at=expires_at)
        )
        async with self._session() as session, session.begin():
            await session.execute(query)

    async def set_pending(self, user_id: UserId, pending: str | None) -> None:
        query = update(users).where(users.c.id == user_id).values(pending=pending)
        async with self._session() as session, session.begin():
            await session.execute(query)

    async def set_retry_context(self, user_id: UserId, context: str | None) -> None:
        query = update(users).where(users.c.id == user_id).values(retry_context=context)
        async with self._session() as session, session.begin():
            await session.execute(query)

    # --- Дневной расход ------------------------------------------------

    async def get_usage(self, user_id: UserId, day: date) -> Usage:
        query = select(usage).where(usage.c.user_id == user_id, usage.c.day == day)
        async with self._session() as session:
            row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            # «Не тратил» и «нет записи» — одно и то же.
            return Usage(day=day)
        return Usage(
            day=row["day"],
            messages_used=row["messages_used"],
            images_used=row["images_used"],
        )

    async def add_usage(
        self,
        user_id: UserId,
        day: date,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> Usage:
        """Атомарный инкремент.

        INSERT … ON CONFLICT DO UPDATE, а не «прочитать и записать»: между
        чтением и записью два одновременных списания затёрли бы друг друга,
        и пользователь получил бы больше, чем ему полагается.
        """
        statement = insert(usage).values(
            user_id=user_id,
            day=day,
            messages_used=messages,
            images_used=images,
        )
        query = statement.on_conflict_do_update(
            index_elements=[usage.c.user_id, usage.c.day],
            set_={
                "messages_used": usage.c.messages_used + messages,
                "images_used": usage.c.images_used + images,
            },
        ).returning(usage)

        async with self._session() as session, session.begin():
            row = (await session.execute(query)).mappings().one()
        return Usage(
            day=row["day"],
            messages_used=row["messages_used"],
            images_used=row["images_used"],
        )

    async def spend_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> bool:
        """Списание всё-или-ничего.

        Условие на достаточность баланса стоит в самом UPDATE. Postgres в
        режиме READ COMMITTED перепроверяет WHERE после захвата блокировки
        строки, поэтому из десяти одновременных попыток при остатке в три
        успешными окажутся ровно три — без явных блокировок с нашей стороны.
        """
        query = (
            update(users)
            .where(
                users.c.id == user_id,
                users.c.bonus_messages >= messages,
                users.c.bonus_images >= images,
            )
            .values(
                bonus_messages=users.c.bonus_messages - messages,
                bonus_images=users.c.bonus_images - images,
            )
            .returning(users.c.id)
        )
        async with self._session() as session, session.begin():
            spent = (await session.execute(query)).one_or_none()
        return spent is not None

    async def add_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> None:
        query = (
            update(users)
            .where(users.c.id == user_id)
            .values(
                bonus_messages=users.c.bonus_messages + messages,
                bonus_images=users.c.bonus_images + images,
            )
        )
        async with self._session() as session, session.begin():
            await session.execute(query)

    # --- Оплата --------------------------------------------------------

    async def create_payment(
        self,
        *,
        user_id: UserId,
        tariff: TariffId,
        method: str,
        amount: int,
        currency: str,
    ) -> Payment:
        payment = Payment(
            id=str(uuid4()),
            user_id=user_id,
            tariff=tariff,
            method=method,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING.value,
            created_at=self._now(),
        )
        async with self._session() as session, session.begin():
            await session.execute(
                insert(payments).values(
                    id=payment.id,
                    user_id=int(user_id),
                    tariff=tariff.value,
                    method=method,
                    amount=amount,
                    currency=currency,
                    status=payment.status,
                    created_at=payment.created_at,
                )
            )
        return payment

    async def get_payment(self, payment_id: str) -> Payment | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(payments).where(payments.c.id == payment_id)
                )
            ).one_or_none()
        return _to_payment(row) if row is not None else None

    async def attach_external_id(self, payment_id: str, external_id: str) -> bool:
        """Привязка, которую защищает ограничение уникальности.

        Нарушение ловим, а не даём упасть: один платёж провайдера не должен
        закрывать два наших заказа, и узнать об этом надо здесь, а не когда
        человек уже заплатил.
        """
        query = (
            update(payments)
            .where(payments.c.id == payment_id)
            .values(external_id=external_id)
            .returning(payments.c.id)
        )
        try:
            async with self._session() as session, session.begin():
                return (await session.execute(query)).one_or_none() is not None
        except IntegrityError:
            return False

    async def mark_paid(self, payment_id: str) -> bool:
        """Переход в «оплачен» ровно один раз.

        Условие на текущий статус стоит в самом UPDATE, поэтому из нескольких
        одновременных уведомлений об оплате выигрывает ровно одно: остальные
        не найдут строку в статусе pending и вернут False. Продлевать подписку
        на каждое уведомление нельзя — их приходит несколько.
        """
        query = (
            update(payments)
            .where(
                payments.c.id == payment_id,
                payments.c.status == PaymentStatus.PENDING.value,
            )
            .values(status=PaymentStatus.PAID.value, paid_at=self._now())
            .returning(payments.c.id)
        )
        async with self._session() as session, session.begin():
            return (await session.execute(query)).one_or_none() is not None

    # --- Диалог --------------------------------------------------------

    async def get_dialog(self, user_id: UserId) -> DialogState:
        query = select(dialogs).where(dialogs.c.user_id == user_id)
        async with self._session() as session:
            row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            return DialogState()
        return DialogState(
            turns=tuple(
                ChatTurn(role=Role(turn["role"]), content=turn["content"])
                for turn in row["turns"]
            ),
            user_turns=row["user_turns"],
        )

    async def save_dialog(self, user_id: UserId, dialog: DialogState) -> None:
        payload: list[dict[str, str]] = [
            {"role": turn.role.value, "content": turn.content} for turn in dialog.turns
        ]
        statement = insert(dialogs).values(
            user_id=user_id, turns=payload, user_turns=dialog.user_turns
        )
        query = statement.on_conflict_do_update(
            index_elements=[dialogs.c.user_id],
            set_={"turns": payload, "user_turns": dialog.user_turns},
        )
        async with self._session() as session, session.begin():
            await session.execute(query)

    async def reset_dialog(self, user_id: UserId) -> None:
        query = delete(dialogs).where(dialogs.c.user_id == user_id)
        async with self._session() as session, session.begin():
            await session.execute(query)

    # --- Рефералы ------------------------------------------------------

    async def record_referral(self, referrer_id: UserId, referee_id: UserId) -> bool:
        """Фиксирует связь. Возвращает False, если она уже была.

        Self-referral отсекается здесь, чтобы вернуть честное False вместо
        исключения. Ограничение в схеме при этом остаётся: оно превращает
        «мы аккуратно проверили» в «этого не может быть».
        """
        if referrer_id == referee_id:
            return False

        statement = insert(referrals).values(
            referee_id=referee_id,
            referrer_id=referrer_id,
            created_at=self._now(),
        )
        query = statement.on_conflict_do_nothing(
            index_elements=[referrals.c.referee_id]
        ).returning(referrals.c.referee_id)

        async with self._session() as session, session.begin():
            recorded = (await session.execute(query)).one_or_none()
        return recorded is not None

    async def count_referrals(self, referrer_id: UserId) -> int:
        query = select(func.count()).where(referrals.c.referrer_id == referrer_id)
        async with self._session() as session:
            return int((await session.execute(query)).scalar_one())

    async def count_referrals_since(self, referrer_id: UserId, since: datetime) -> int:
        query = select(func.count()).where(
            referrals.c.referrer_id == referrer_id,
            referrals.c.created_at >= since,
        )
        async with self._session() as session:
            return int((await session.execute(query)).scalar_one())

    # --- Внутреннее ----------------------------------------------------

    async def _fetch_user(self, query: Any) -> User | None:
        async with self._session() as session:
            row = (await session.execute(query)).mappings().one_or_none()
        return None if row is None else _to_user(row)


def _to_payment(row: Any) -> Payment:
    return Payment(
        id=row.id,
        user_id=UserId(row.user_id),
        tariff=TariffId(row.tariff),
        method=row.method,
        amount=row.amount,
        currency=row.currency,
        status=row.status,
        created_at=row.created_at,
        external_id=row.external_id,
        paid_at=row.paid_at,
    )


def _to_user(row: Any) -> User:
    """Собирает доменного пользователя из строки таблицы."""
    return User(
        id=UserId(row["id"]),
        messenger=MessengerKind(row["messenger"]),
        external_id=row["external_id"],
        tariff=TariffId(row["tariff"]),
        referral_code=row["referral_code"],
        created_at=row["created_at"],
        daily_image_quota=row["daily_image_quota"],
        referred_by=None if row["referred_by"] is None else UserId(row["referred_by"]),
        bonus_messages=row["bonus_messages"],
        bonus_images=row["bonus_images"],
        tariff_expires_at=row["tariff_expires_at"],
        pending=row["pending"],
        retry_context=row["retry_context"],
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
