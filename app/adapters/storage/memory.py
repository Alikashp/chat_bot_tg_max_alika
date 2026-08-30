"""Хранилище в памяти процесса.

Используется в тестах и на фазе 1, пока в проекте нет ни лимитов, ни денег.
Для прода не годится: Railway при каждой выкатке поднимает новый контейнер,
и всё содержимое памяти теряется вместе с оплаченными подписками
(docs/research.md §5). С фазы 3 прод работает на PostgreSQL.

Об атомарности. Все операции здесь выполняются без единого await внутри
критической секции, поэтому в однопоточном event loop они атомарны по
построению: другая корутина не может вклиниться в середину. Именно это
проверяют параллельные тесты в tests/contract/test_storage.py — они же
поймают потерю атомарности в реализации на PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import count
from uuid import uuid4

from app.core.models import (
    DialogState,
    MessengerKind,
    Payment,
    TariffId,
    Usage,
    User,
    UserId,
)
from app.ports.payments import PaymentStatus


class InMemoryStorage:
    """Реализация порта Storage поверх обычных словарей."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        """
        Args:
            clock: Источник времени. Нужен тестам про суточные окна: без него
                хранилище писало бы отметки по настоящим часам, пока
                сценарий живёт по подставным, и проверка «потолок наград
                сбрасывается назавтра» молча ничего бы не проверяла.
        """
        self._now = clock if clock is not None else _utc_now
        self._ids = count(1)
        self._users: dict[UserId, User] = {}
        self._by_external: dict[tuple[MessengerKind, str], UserId] = {}
        self._by_referral_code: dict[str, UserId] = {}
        self._usage: dict[tuple[UserId, date], Usage] = {}
        self._dialogs: dict[UserId, DialogState] = {}
        #: Приглашённый -> (пригласивший, когда). Ключ по приглашённому,
        #: потому что награда полагается только за нового пользователя и
        #: только одному пригласившему.
        self._referrals: dict[UserId, tuple[UserId, datetime]] = {}
        self._payments: dict[str, Payment] = {}

    # --- Пользователи --------------------------------------------------

    async def get_user(self, messenger: MessengerKind, external_id: str) -> User | None:
        user_id = self._by_external.get((messenger, external_id))
        return None if user_id is None else self._users[user_id]

    async def get_user_by_id(self, user_id: UserId) -> User | None:
        return self._users.get(user_id)

    async def get_user_by_referral_code(self, code: str) -> User | None:
        user_id = self._by_referral_code.get(code)
        return None if user_id is None else self._users[user_id]

    async def create_user(
        self,
        *,
        messenger: MessengerKind,
        external_id: str,
        referral_code: str,
        daily_image_quota: int,
        referred_by: UserId | None = None,
    ) -> User:
        if (messenger, external_id) in self._by_external:
            raise ValueError(
                f"пользователь {messenger.value}:{external_id} уже существует"
            )
        if referral_code in self._by_referral_code:
            raise ValueError(f"реферальный код {referral_code} уже занят")

        user = User(
            id=UserId(next(self._ids)),
            messenger=messenger,
            external_id=external_id,
            tariff=TariffId.FREE,
            referral_code=referral_code,
            created_at=self._now(),
            daily_image_quota=daily_image_quota,
            referred_by=referred_by,
        )
        self._users[user.id] = user
        self._by_external[(messenger, external_id)] = user.id
        self._by_referral_code[referral_code] = user.id
        return user

    async def set_tariff(
        self,
        user_id: UserId,
        tariff: TariffId,
        expires_at: datetime | None,
    ) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(
            user, tariff=tariff, tariff_expires_at=expires_at
        )

    async def set_pending(self, user_id: UserId, pending: str | None) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, pending=pending)

    async def set_retry_context(self, user_id: UserId, context: str | None) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, retry_context=context)

    # --- Дневной расход ------------------------------------------------

    async def get_usage(self, user_id: UserId, day: date) -> Usage:
        self._require_user(user_id)
        return self._usage.get((user_id, day), Usage(day=day))

    async def add_usage(
        self,
        user_id: UserId,
        day: date,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> Usage:
        self._require_user(user_id)
        current = self._usage.get((user_id, day), Usage(day=day))
        updated = Usage(
            day=day,
            messages_used=current.messages_used + messages,
            images_used=current.images_used + images,
        )
        self._usage[(user_id, day)] = updated
        return updated

    async def spend_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> bool:
        user = self._require_user(user_id)
        if user.bonus_messages < messages or user.bonus_images < images:
            return False
        self._users[user_id] = replace(
            user,
            bonus_messages=user.bonus_messages - messages,
            bonus_images=user.bonus_images - images,
        )
        return True

    async def add_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
    ) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(
            user,
            bonus_messages=user.bonus_messages + messages,
            bonus_images=user.bonus_images + images,
        )

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
        self._payments[payment.id] = payment
        return payment

    async def get_payment(self, payment_id: str) -> Payment | None:
        return self._payments.get(payment_id)

    async def attach_external_id(self, payment_id: str, external_id: str) -> bool:
        taken = any(
            other.external_id == external_id and other.id != payment_id
            for other in self._payments.values()
        )
        payment = self._payments.get(payment_id)
        if taken or payment is None:
            return False
        self._payments[payment_id] = replace(payment, external_id=external_id)
        return True

    async def mark_paid(self, payment_id: str) -> bool:
        payment = self._payments.get(payment_id)
        if payment is None or payment.status != PaymentStatus.PENDING.value:
            return False
        self._payments[payment_id] = replace(
            payment, status=PaymentStatus.PAID.value, paid_at=self._now()
        )
        return True

    # --- Диалог --------------------------------------------------------

    async def get_dialog(self, user_id: UserId) -> DialogState:
        self._require_user(user_id)
        return self._dialogs.get(user_id, DialogState())

    async def save_dialog(self, user_id: UserId, dialog: DialogState) -> None:
        self._require_user(user_id)
        self._dialogs[user_id] = dialog

    async def reset_dialog(self, user_id: UserId) -> None:
        self._require_user(user_id)
        self._dialogs.pop(user_id, None)

    # --- Рефералы ------------------------------------------------------

    async def record_referral(self, referrer_id: UserId, referee_id: UserId) -> bool:
        if referrer_id == referee_id:
            return False
        self._require_user(referrer_id)
        self._require_user(referee_id)
        if referee_id in self._referrals:
            return False
        self._referrals[referee_id] = (referrer_id, self._now())
        return True

    async def count_referrals(self, referrer_id: UserId) -> int:
        return sum(
            1 for referrer, _ in self._referrals.values() if referrer == referrer_id
        )

    async def count_referrals_since(self, referrer_id: UserId, since: datetime) -> int:
        return sum(
            1
            for referrer, recorded_at in self._referrals.values()
            if referrer == referrer_id and recorded_at >= since
        )

    # --- Внутреннее ----------------------------------------------------

    def _require_user(self, user_id: UserId) -> User:
        user = self._users.get(user_id)
        if user is None:
            raise KeyError(f"нет пользователя с id={user_id}")
        return user


def _utc_now() -> datetime:
    """Настоящие часы — значение по умолчанию."""
    return datetime.now(UTC)
