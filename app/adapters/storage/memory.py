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
from datetime import UTC, date, datetime, timedelta
from itertools import count
from uuid import uuid4

from app.core import sources
from app.core.generations import Generation
from app.core.gifts import Gift, GiftGrant
from app.core.limits import LimitKind
from app.core.models import (
    NO_USERNAME,
    DialogState,
    Mailing,
    MailingStatus,
    MessengerKind,
    Payment,
    PeriodUsage,
    StarCancel,
    Subscription,
    TariffId,
    Usage,
    User,
    UserId,
)
from app.ports.payments import PaymentMethod, PaymentStatus, SubscriptionStatus
from app.ports.storage import GrantOutcome

#: Какое поле расхода за период отвечает за какой вид нормы.
_PERIOD_FIELDS: dict[LimitKind, str] = {
    LimitKind.IMAGES: "images_used",
    LimitKind.DOCUMENTS: "documents_used",
    LimitKind.PRESENTATIONS: "presentations_used",
}


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
        self._period_usage: dict[tuple[UserId, datetime], PeriodUsage] = {}
        self._dialogs: dict[UserId, DialogState] = {}
        self._dialogs_touched: dict[UserId, datetime] = {}
        self._first_decks: dict[UserId, datetime] = {}
        self._mailings: dict[tuple[UserId, str, datetime], Mailing] = {}
        self._gifts: list[Gift] = []
        #: Учёт обращений к провайдерам. Публичный намеренно: в тестах по
        #: нему проверяют, что попытка записана, — так же, как в бою по
        #: таблице generations.
        self.generations: list[Generation] = []
        #: Когда человеку выдали разовую презентацию (фаза 10, К6). В базе это
        #: колонка users.presentations_granted_at; здесь хватает словаря —
        #: доменному пользователю отметка ни к чему, ею пользуется только
        #: раздача.
        self.presentations_granted_at: dict[UserId, datetime] = {}
        #: Приглашённый -> (пригласивший, когда). Ключ по приглашённому,
        #: потому что награда полагается только за нового пользователя и
        #: только одному пригласившему.
        self._referrals: dict[UserId, tuple[UserId, datetime]] = {}
        self._payments: dict[str, Payment] = {}
        self._subscriptions: dict[UserId, Subscription] = {}
        self._star_cancels: dict[str, StarCancel] = {}
        self._star_ledger_offset = 0

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
        support_number: int,
        bonus_images: int,
        bonus_documents: int,
        username: str = NO_USERNAME,
        source: str = sources.DIRECT,
        bonus_presentations: int = 0,
    ) -> User:
        existing = self._by_external.get((messenger, external_id))
        if existing is not None:
            # Не ошибка: кто-то успел завести его первым.
            return self._users[existing]
        if referral_code in self._by_referral_code:
            raise ValueError(f"реферальный код {referral_code} уже занят")
        if any(
            other.support_number == support_number for other in self._users.values()
        ):
            raise ValueError(f"номер {support_number} уже занят")

        user = User(
            id=UserId(next(self._ids)),
            messenger=messenger,
            external_id=external_id,
            tariff=TariffId.FREE,
            referral_code=referral_code,
            support_number=support_number,
            created_at=self._now(),
            last_active_at=self._now(),
            bonus_images=bonus_images,
            bonus_documents=bonus_documents,
            bonus_presentations=bonus_presentations,
            username=username,
            source=source,
        )
        self.presentations_granted_at[user.id] = self._now()
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

    async def set_username(self, user_id: UserId, username: str) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, username=username)

    async def set_email(self, user_id: UserId, email: str) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, email=email)

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
    ) -> Usage:
        self._require_user(user_id)
        current = self._usage.get((user_id, day), Usage(day=day))
        updated = Usage(day=day, messages_used=current.messages_used + messages)
        self._usage[(user_id, day)] = updated
        return updated

    async def get_period_usage(
        self, user_id: UserId, period_start: datetime
    ) -> PeriodUsage:
        self._require_user(user_id)
        return self._period_usage.get((user_id, period_start), PeriodUsage())

    async def spend_norm(
        self,
        user_id: UserId,
        period_start: datetime,
        kind: LimitKind,
        *,
        limit: int,
    ) -> bool:
        # Ни одного await между проверкой и записью: в памяти это и есть
        # атомарность.
        self._require_user(user_id)
        field = _PERIOD_FIELDS[kind]
        current = self._period_usage.get((user_id, period_start), PeriodUsage())
        if getattr(current, field) >= limit:
            return False
        self._period_usage[(user_id, period_start)] = replace(
            current, **{field: getattr(current, field) + 1}
        )
        return True

    async def spend_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
        documents: int = 0,
        presentations: int = 0,
    ) -> bool:
        user = self._require_user(user_id)
        if (
            user.bonus_messages < messages
            or user.bonus_images < images
            or user.bonus_documents < documents
            or user.bonus_presentations < presentations
        ):
            return False
        self._users[user_id] = replace(
            user,
            bonus_messages=user.bonus_messages - messages,
            bonus_images=user.bonus_images - images,
            bonus_documents=user.bonus_documents - documents,
            bonus_presentations=user.bonus_presentations - presentations,
        )
        return True

    async def add_bonus(
        self,
        user_id: UserId,
        *,
        messages: int = 0,
        images: int = 0,
        documents: int = 0,
        presentations: int = 0,
    ) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(
            user,
            bonus_messages=user.bonus_messages + messages,
            bonus_images=user.bonus_images + images,
            bonus_documents=user.bonus_documents + documents,
            bonus_presentations=user.bonus_presentations + presentations,
        )

    async def grant_gift(
        self,
        user_id: UserId,
        kind: LimitKind,
        *,
        source: str,
        granted_at: datetime,
        expires_at: datetime,
    ) -> None:
        self._require_user(user_id)
        self._gifts.append(
            Gift(
                user_id=user_id,
                kind=kind,
                source=source,
                granted_at=granted_at,
                expires_at=expires_at,
            )
        )

    def _live_gifts(self, user_id: UserId, kind: LimitKind, now: datetime) -> list[int]:
        """Номера подарков, которые можно потратить, — ранний срок первым."""
        live = [
            index
            for index, gift in enumerate(self._gifts)
            if gift.user_id == user_id
            and gift.kind is kind
            and gift.used_at is None
            and gift.expired_at is None
            and gift.expires_at > now
        ]
        return sorted(live, key=lambda index: self._gifts[index].expires_at)

    async def count_gifts(self, user_id: UserId, kind: LimitKind, now: datetime) -> int:
        return len(self._live_gifts(user_id, kind, now))

    async def spend_gift(self, user_id: UserId, kind: LimitKind, now: datetime) -> bool:
        live = self._live_gifts(user_id, kind, now)
        if not live:
            return False
        self._gifts[live[0]] = replace(self._gifts[live[0]], used_at=now)
        return True

    async def expire_gifts(self, now: datetime) -> int:
        expired = 0
        for index, gift in enumerate(self._gifts):
            if (
                gift.used_at is None
                and gift.expired_at is None
                and gift.expires_at <= now
            ):
                self._gifts[index] = replace(gift, expired_at=gift.expires_at)
                expired += 1
        return expired

    async def gifts_of(self, user_id: UserId) -> list[Gift]:
        return [gift for gift in self._gifts if gift.user_id == user_id]

    async def claim_presentation(
        self, user_id: UserId, now: datetime, *, stale_after: timedelta
    ) -> bool:
        user = self._require_user(user_id)
        started = user.presentation_started_at
        if started is not None and started >= now - stale_after:
            return False
        self._users[user_id] = replace(user, presentation_started_at=now)
        return True

    async def set_menu_version(self, user_id: UserId, version: str) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, menu_version=version)

    async def release_presentation(self, user_id: UserId) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, presentation_started_at=None)

    async def record_generation(self, generation: Generation) -> None:
        self._require_user(generation.user_id)
        self.generations.append(generation)

    async def mark_stopped(self, user_id: UserId, at: datetime) -> None:
        user = self._require_user(user_id)
        if user.stopped_at is None:
            self._users[user_id] = replace(user, stopped_at=at)

    async def clear_stopped(self, user_id: UserId) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, stopped_at=None)

    async def touch_active(self, user_id: UserId, at: datetime) -> None:
        user = self._require_user(user_id)
        if user.last_active_at is None or user.last_active_at < at:
            self._users[user_id] = replace(user, last_active_at=at)

    async def claim_mailing(
        self, user_id: UserId, step: str, cycle_key: datetime, at: datetime
    ) -> bool:
        self._require_user(user_id)
        key = (user_id, step, cycle_key)
        if key in self._mailings:
            return False
        self._mailings[key] = Mailing(
            user_id=user_id,
            step=step,
            cycle_key=cycle_key,
            sent_at=at,
            status=MailingStatus.SENDING,
        )
        return True

    async def finish_mailing(
        self,
        user_id: UserId,
        step: str,
        cycle_key: datetime,
        *,
        status: MailingStatus,
        at: datetime,
        error_code: str | None = None,
        gift: GiftGrant | None = None,
    ) -> bool:
        # Ни одного await между проверкой и записями: в памяти это и есть
        # одна транзакция.
        key = (user_id, step, cycle_key)
        row = self._mailings[key]
        if row.status is not MailingStatus.SENDING:
            return False
        user = self._require_user(user_id)
        self._mailings[key] = replace(
            row, status=status, error_code=error_code, sent_at=at
        )
        if status is MailingStatus.SENT:
            self._users[user_id] = replace(user, last_marketing_at=at)
            if gift is not None:
                self._gifts.append(
                    Gift(
                        user_id=user_id,
                        kind=gift.kind,
                        source=gift.source,
                        granted_at=at,
                        expires_at=gift.expires_at,
                    )
                )
        return True

    async def mark_mailing_clicked(
        self, user_id: UserId, step: str, at: datetime
    ) -> bool:
        mine = [
            row
            for row in self._mailings.values()
            if row.user_id == user_id and row.step == step
        ]
        if not mine:
            return False
        latest = max(mine, key=lambda row: row.sent_at)
        if latest.clicked_at is not None:
            return False
        key = (user_id, step, latest.cycle_key)
        self._mailings[key] = replace(latest, clicked_at=at)
        return True

    async def mailings_of(self, user_id: UserId) -> list[Mailing]:
        return sorted(
            (row for row in self._mailings.values() if row.user_id == user_id),
            key=lambda row: row.sent_at,
        )

    async def mailing_candidates(
        self,
        *,
        messenger: MessengerKind,
        step: str,
        active_after: datetime,
        active_before: datetime,
        marketing_before: datetime,
        now: datetime,
        limit: int,
    ) -> list[User]:
        found = [
            user
            for user in self._users.values()
            if user.messenger is messenger
            and user.stopped_at is None
            and user.last_active_at is not None
            and active_after <= user.last_active_at < active_before
            and (
                user.tariff is TariffId.FREE
                or user.tariff_expires_at is None
                or user.tariff_expires_at <= now
            )
            and (
                user.last_marketing_at is None
                or user.last_marketing_at < marketing_before
            )
            and (user.id, step, user.last_active_at) not in self._mailings
        ]
        return sorted(found, key=lambda user: user.id)[:limit]

    async def mark_first_deck(self, user_id: UserId, at: datetime) -> bool:
        self._require_user(user_id)
        if user_id in self._first_decks:
            return False
        self._first_decks[user_id] = at
        return True

    async def mark_free_regen_used(self, user_id: UserId, at: datetime) -> bool:
        user = self._require_user(user_id)
        if user.free_regen_at is not None:
            return False
        self._users[user_id] = replace(user, free_regen_at=at)
        return True

    async def remember_channel_check(self, user_id: UserId, at: datetime) -> None:
        user = self._require_user(user_id)
        self._users[user_id] = replace(user, channel_checked_at=at)

    # --- Оплата --------------------------------------------------------

    async def create_payment(
        self,
        *,
        user_id: UserId,
        tariff: TariffId,
        method: str,
        amount: int,
        currency: str,
        docs_version: str,
        trial: bool = False,
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
            docs_version=docs_version,
            trial=trial,
        )
        self._payments[payment.id] = payment
        return payment

    async def open_or_create_payment(
        self,
        *,
        user_id: UserId,
        tariff: TariffId,
        method: str,
        amount: int,
        currency: str,
        docs_version: str,
        trial: bool,
        linked_after: datetime,
        making_after: datetime,
    ) -> tuple[Payment, bool]:
        # Ни одного await между поиском и записью: в памяти это и есть
        # атомарность.
        live = [
            order
            for order in self._payments.values()
            if order.user_id == user_id
            and order.status == PaymentStatus.PENDING.value
            and order.tariff is tariff
            and order.method == method
            and order.trial is trial
            and (
                order.created_at >= linked_after
                if order.confirmation_url is not None
                else order.created_at >= making_after
            )
        ]
        if live:
            return max(live, key=lambda order: order.created_at), False
        payment = Payment(
            id=str(uuid4()),
            user_id=user_id,
            tariff=tariff,
            method=method,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING.value,
            created_at=self._now(),
            docs_version=docs_version,
            trial=trial,
        )
        self._payments[payment.id] = payment
        return payment, True

    async def remember_link(
        self, payment_id: str, *, url: str, receipt_email: str | None
    ) -> None:
        order = self._payments[payment_id]
        self._payments[payment_id] = replace(
            order, confirmation_url=url, receipt_email=receipt_email
        )

    async def mark_canceled(self, payment_id: str) -> bool:
        order = self._payments.get(payment_id)
        if order is None or order.status != PaymentStatus.PENDING.value:
            return False
        self._payments[payment_id] = replace(order, status=PaymentStatus.CANCELED.value)
        return True

    async def linked_orders(
        self, user_id: UserId, *, method: str, created_after: datetime
    ) -> list[Payment]:
        found = [
            order
            for order in self._payments.values()
            if order.user_id == user_id
            and order.status == PaymentStatus.PENDING.value
            and order.method == method
            and order.confirmation_url is not None
            and order.created_at >= created_after
        ]
        return sorted(found, key=lambda order: order.created_at, reverse=True)

    def orders_of(self, user_id: UserId) -> list[Payment]:
        """Все заказы человека по порядку — только для тестов."""
        return sorted(
            (order for order in self._payments.values() if order.user_id == user_id),
            key=lambda order: order.created_at,
        )

    async def ever_paid(self, user_id: UserId) -> bool:
        return any(
            payment.user_id == user_id
            and payment.status
            in (PaymentStatus.PAID.value, PaymentStatus.REFUNDED.value)
            for payment in self._payments.values()
        )

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

    async def complete_payment(
        self,
        payment_id: str,
        *,
        tariff: TariffId,
        expires_at: datetime,
        seen_tariff: TariffId,
        seen_expiry: datetime | None,
        subscription: Subscription | None,
        norm_since: datetime,
        trial: bool = False,
    ) -> GrantOutcome:
        # Ни одного await внутри: в памяти это и есть транзакция.
        payment = self._payments.get(payment_id)
        if payment is None or payment.status != PaymentStatus.PENDING.value:
            return GrantOutcome.ALREADY
        user = self._require_user(payment.user_id)
        if trial and self._paid_before(payment):
            self._payments[payment_id] = replace(
                payment, status=PaymentStatus.PAID.value, paid_at=self._now()
            )
            return GrantOutcome.TRIAL_USED
        if user.tariff is not seen_tariff or user.tariff_expires_at != seen_expiry:
            return GrantOutcome.STALE
        self._payments[payment_id] = replace(
            payment, status=PaymentStatus.PAID.value, paid_at=self._now()
        )
        self._users[user.id] = replace(
            user,
            tariff=tariff,
            tariff_expires_at=expires_at,
            norm_since=norm_since,
            trial_order_id=payment_id if trial else user.trial_order_id,
        )
        if subscription is not None:
            self._subscriptions[subscription.user_id] = subscription
        return GrantOutcome.GRANTED

    def _paid_before(self, payment: Payment) -> bool:
        """Есть ли у человека другой оплаченный или возвращённый заказ."""
        return any(
            other.user_id == payment.user_id
            and other.id != payment.id
            and other.status in (PaymentStatus.PAID.value, PaymentStatus.REFUNDED.value)
            for other in self._payments.values()
        )

    async def payments_to_reconcile(
        self, *, created_before: datetime, created_after: datetime, limit: int
    ) -> list[Payment]:
        due = [
            payment
            for payment in self._payments.values()
            if payment.status == PaymentStatus.PENDING.value
            and payment.external_id is not None
            and payment.method == PaymentMethod.CARD.value
            and created_after <= payment.created_at <= created_before
        ]
        return sorted(due, key=lambda payment: payment.created_at)[:limit]

    async def mark_paid(self, payment_id: str) -> bool:
        payment = self._payments.get(payment_id)
        if payment is None or payment.status != PaymentStatus.PENDING.value:
            return False
        self._payments[payment_id] = replace(
            payment, status=PaymentStatus.PAID.value, paid_at=self._now()
        )
        return True

    async def get_payment_by_external_id(self, external_id: str) -> Payment | None:
        return next(
            (
                payment
                for payment in self._payments.values()
                if payment.external_id == external_id
            ),
            None,
        )

    async def mark_refunded(self, payment_id: str) -> bool:
        payment = self._payments.get(payment_id)
        if payment is None or payment.status != PaymentStatus.PAID.value:
            return False
        self._payments[payment_id] = replace(
            payment, status=PaymentStatus.REFUNDED.value
        )
        return True

    # --- Подписка ------------------------------------------------------

    async def get_subscription(self, user_id: UserId) -> Subscription | None:
        return self._subscriptions.get(user_id)

    async def save_subscription(self, subscription: Subscription) -> None:
        self._subscriptions[subscription.user_id] = subscription

    async def advance_subscription(
        self,
        user_id: UserId,
        *,
        next_charge_at: datetime,
        status: str,
        failed_since: datetime | None,
        amount: int | None = None,
        same_charge: bool = False,
    ) -> bool:
        current = self._subscriptions.get(user_id)
        if current is None or current.status == SubscriptionStatus.CANCELLED.value:
            return False
        self._subscriptions[user_id] = replace(
            current,
            next_charge_at=next_charge_at,
            status=status,
            failed_since=failed_since,
            amount=current.amount if amount is None else amount,
            reminded_for=next_charge_at if same_charge else current.reminded_for,
            price_checked_for=(
                next_charge_at if same_charge else current.price_checked_for
            ),
        )
        return True

    async def hold_charge_order(self, user_id: UserId, order_id: str) -> bool:
        current = self._subscriptions.get(user_id)
        if (
            current is None
            or current.status == SubscriptionStatus.CANCELLED.value
            or current.charge_order_id is not None
        ):
            return False
        self._subscriptions[user_id] = replace(current, charge_order_id=order_id)
        return True

    async def release_charge_order(self, user_id: UserId, order_id: str) -> None:
        current = self._subscriptions.get(user_id)
        if current is not None and current.charge_order_id == order_id:
            self._subscriptions[user_id] = replace(current, charge_order_id=None)

    async def cancel_subscription(self, user_id: UserId, at: datetime) -> bool:
        current = self._subscriptions.get(user_id)
        if current is None or current.status == SubscriptionStatus.CANCELLED.value:
            return False
        self._subscriptions[user_id] = replace(
            current,
            status=SubscriptionStatus.CANCELLED.value,
            cancelled_at=at,
            # Забываем способ оплаты, а не только помечаем статус: у ЮKassa
            # сохранённую карту не удалить, платежи по ней идут, пока мы их
            # создаём, и отключение автоплатежа целиком на нашей стороне.
            payment_method_id=None,
        )
        return True

    async def subscriptions_to_charge(
        self, now: datetime, *, limit: int
    ) -> list[Subscription]:
        due = [
            subscription
            for subscription in self._subscriptions.values()
            if subscription.status != SubscriptionStatus.CANCELLED.value
            and subscription.next_charge_at <= now
        ]
        due.sort(key=lambda subscription: subscription.next_charge_at)
        return due[:limit]

    async def subscriptions_to_remind(
        self, since: datetime, until: datetime, *, limit: int
    ) -> list[Subscription]:
        due = [
            subscription
            for subscription in self._subscriptions.values()
            if subscription.status == SubscriptionStatus.ACTIVE.value
            and subscription.remind_before_charge
            and since < subscription.next_charge_at <= until
            and subscription.reminded_for != subscription.next_charge_at
        ]
        due.sort(key=lambda subscription: subscription.next_charge_at)
        return due[:limit]

    async def mark_reminded(self, user_id: UserId, charge_at: datetime) -> None:
        current = self._subscriptions.get(user_id)
        if current is not None:
            self._subscriptions[user_id] = replace(current, reminded_for=charge_at)

    async def subscriptions_to_check_price(
        self, since: datetime, until: datetime, *, limit: int
    ) -> list[Subscription]:
        due = [
            subscription
            for subscription in self._subscriptions.values()
            if subscription.status == SubscriptionStatus.ACTIVE.value
            and since < subscription.next_charge_at <= until
            and subscription.price_checked_for != subscription.next_charge_at
        ]
        due.sort(key=lambda subscription: subscription.next_charge_at)
        return due[:limit]

    async def mark_price_checked(self, user_id: UserId, charge_at: datetime) -> None:
        current = self._subscriptions.get(user_id)
        if current is not None:
            self._subscriptions[user_id] = replace(current, price_checked_for=charge_at)

    async def queue_star_cancel(
        self, user_id: UserId, charge_id: str, at: datetime
    ) -> None:
        self._require_user(user_id)
        self._star_cancels.setdefault(charge_id, StarCancel(user_id, charge_id, at))

    async def star_cancels_due(self, *, limit: int) -> list[StarCancel]:
        due = sorted(self._star_cancels.values(), key=lambda c: c.queued_at)
        return due[:limit]

    async def star_cancel_done(self, charge_id: str) -> None:
        self._star_cancels.pop(charge_id, None)

    async def star_ledger_offset(self) -> int:
        return self._star_ledger_offset

    async def set_star_ledger_offset(self, offset: int) -> None:
        self._star_ledger_offset = offset

    # --- Диалог --------------------------------------------------------

    async def get_dialog(self, user_id: UserId) -> DialogState:
        self._require_user(user_id)
        return self._dialogs.get(user_id, DialogState())

    async def save_dialog(self, user_id: UserId, dialog: DialogState) -> None:
        self._require_user(user_id)
        self._dialogs[user_id] = dialog
        self._dialogs_touched[user_id] = self._now()

    async def reset_dialog(self, user_id: UserId) -> None:
        self._require_user(user_id)
        self._dialogs.pop(user_id, None)
        self._dialogs_touched.pop(user_id, None)

    async def forget_idle_dialogs(self, before: datetime) -> int:
        idle = [who for who, at in self._dialogs_touched.items() if at < before]
        for who in idle:
            self._dialogs.pop(who, None)
            del self._dialogs_touched[who]
        return len(idle)

    # --- Рефералы ------------------------------------------------------

    async def record_referral(
        self,
        referrer_id: UserId,
        referee_id: UserId,
        *,
        since: datetime | None = None,
        limit: int = 0,
    ) -> bool:
        if referrer_id == referee_id:
            return False
        self._require_user(referrer_id)
        self._require_user(referee_id)
        if referee_id in self._referrals:
            return False
        # Без await между подсчётом и записью: в одной петле это одна операция.
        if (
            limit > 0
            and since is not None
            and self._count_since(referrer_id, since) >= limit
        ):
            return False
        self._referrals[referee_id] = (referrer_id, self._now())
        return True

    async def count_referrals(self, referrer_id: UserId) -> int:
        return sum(
            1 for referrer, _ in self._referrals.values() if referrer == referrer_id
        )

    async def count_referrals_since(self, referrer_id: UserId, since: datetime) -> int:
        return self._count_since(referrer_id, since)

    def _count_since(self, referrer_id: UserId, since: datetime) -> int:
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
