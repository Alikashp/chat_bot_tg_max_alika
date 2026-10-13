"""Сверка зависших заказов (фаза 11, П5).

Тариф выдаётся по уведомлению ЮKassa, но уведомление — не гарантия. Оно могло
не прийти; могло прийти, а перечитать платёж в тот момент не вышло; могло
встать в очередь, после чего процесс перезапустился. Во втором и третьем
случае ЮKassa его не повторит: 200 мы ей уже ответили. Человек заплатил и
остался без тарифа.

Поэтому раз в несколько минут — проход по заказам картой, которые ждут
дольше обычного: перечитать платёж ключом магазина того мессенджера, откуда
человек, и довести тем же подтверждением, что и уведомление. Выдача там
атомарна и однократна (П4), так что сверка и запоздавшее уведомление не
выдадут тариф дважды.

Звёзды сверяются по журналу транзакций бота (сессия 10). Сообщение об
оплате Telegram присылает один раз: упала обработка или процесс
перезапустился, пока сообщение ждало в очереди, — повтора не будет. Журнал
же помнит каждую оплату. Сверка читает его от позиции, сохранённой в базе,
и доводит каждую оплату тем же подтверждением; позиция не уходит дальше
оплаты, которую довести не вышло.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from app.core.generations import error_code
from app.core.models import MessengerKind, Payment
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, session_for
from app.ports.payments import PaymentMethod, PaymentStatus, StarPayment

#: Сколько транзакций журнала читать за запрос: больше Telegram не отдаёт.
STAR_LEDGER_PAGE = 100

#: Продление не случается в первые сутки после первой оплаты: период — 30
#: дней. Транзакция этого окна у заказа, который до сессии 10 не запоминал
#: своё списание, — сама первая оплата, а не продление.
_FIRST_PAYMENT_WINDOW = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Reconciler:
    """Один проход сверки."""

    #: Зависимости по мессенджерам: у каждого свой магазин ЮKassa (П6).
    by_messenger: Mapping[MessengerKind, Deps]
    #: С какого возраста заказ считается зависшим. Моложе — ещё ждём
    #: уведомления: оно обычно приходит за секунды.
    after: timedelta
    #: До какого возраста перечитываем. Старше — неоплаченный счёт давно
    #: отменён у провайдера, и спрашивать о нём каждые пять минут незачем.
    window: timedelta
    batch: int = 50

    def __post_init__(self) -> None:
        if not self.by_messenger:
            raise ValueError("нужен хотя бы один мессенджер")

    async def run(self) -> None:
        """Один проход: заказы картой, потом журнал звёзд."""
        await self._cards()
        try:
            await self._stars()
        except Exception as error:
            # Позиция журнала осталась на оплате, которую не довели: следующий
            # проход начнёт с неё же.
            next(iter(self.by_messenger.values())).logger.warning(
                "star_reconcile_failed", error=error_code(error)
            )

    async def _cards(self) -> None:
        """Зависшие заказы картой. Сбой на одном заказе не мешает остальным."""
        any_deps = next(iter(self.by_messenger.values()))
        now = any_deps.now()
        due = await any_deps.storage.payments_to_reconcile(
            created_before=now - self.after,
            created_after=now - self.window,
            limit=self.batch,
        )
        for order in due:
            try:
                await self._settle(order)
            except Exception as error:
                any_deps.logger.warning(
                    "reconcile_failed",
                    user_id=int(order.user_id),
                    payment_id=order.id,
                    error=error_code(error),
                )

    async def _settle(self, order: Payment) -> None:
        any_deps = next(iter(self.by_messenger.values()))
        user = await any_deps.storage.get_user_by_id(order.user_id)
        if user is None or order.external_id is None:
            return
        deps = self.by_messenger.get(user.messenger)
        if deps is None or deps.cards is None:
            # Магазин этого мессенджера не настроен — перечитать нечем.
            deps_or_any = deps or any_deps
            deps_or_any.logger.error("reconcile_without_provider", user_id=int(user.id))
            return
        if not await deps.cards.is_paid(order.external_id, expected_rub=order.amount):
            return
        # Заказ может оказаться автосписанием за период, ответ на которое
        # потерялся. Узнавать это надо до выдачи: она освобождает период.
        renewal = await payments.is_card_renewal(deps, order)
        confirmed = await payments.confirm(deps, order.id)
        if confirmed is None:
            return
        deps.logger.info(
            "payment_reconciled", user_id=int(user.id), payment_id=order.id
        )
        await payments.announce(
            deps, session_for(deps, user), confirmed, renewal=renewal
        )

    async def _stars(self) -> None:
        """Журнал звёздных транзакций — от сохранённой позиции до конца."""
        deps = self.by_messenger.get(MessengerKind.TELEGRAM)
        if deps is None or deps.stars is None:
            return
        offset = await deps.storage.star_ledger_offset()
        while True:
            page = await deps.stars.ledger(offset=offset, limit=STAR_LEDGER_PAGE)
            for payment in page.payments:
                await self._settle_star(deps, payment)
            offset += page.read
            await deps.storage.set_star_ledger_offset(offset)
            if page.read < STAR_LEDGER_PAGE:
                return

    async def _settle_star(self, deps: Deps, paid: StarPayment) -> None:
        """Доводит одну оплату звёздами, если её ещё не довели.

        Чужое и непонятное пропускается с записью в лог: такая транзакция не
        должна держать позицию журнала вечно. Исключение — только сбой
        выдачи: тогда позиция стоит, и оплата сверится снова.
        """
        first = await deps.storage.get_payment(paid.order_id)
        user = (
            await deps.storage.get_user_by_id(first.user_id)
            if first is not None
            else None
        )
        if (
            first is None
            or user is None
            or first.method != PaymentMethod.STARS.value
            or user.external_id != paid.user_id
        ):
            deps.logger.warning("star_payment_unmatched")
            return

        known = await deps.storage.get_payment_by_external_id(paid.charge_id)
        if known is not None and known.status != PaymentStatus.PENDING.value:
            return
        if _legacy_first_payment(first, paid):
            return

        renewal = not (
            first.status == PaymentStatus.PENDING.value
            or (known is not None and known.id == first.id)
        )
        confirmed = await payments.confirm(
            deps, first.id, charge_id=paid.charge_id, renewal=renewal
        )
        if confirmed is None:
            return
        deps.logger.info(
            "star_payment_reconciled", user_id=int(user.id), payment_id=confirmed.id
        )
        await payments.announce(
            deps, session_for(deps, user), confirmed, renewal=renewal
        )


def _legacy_first_payment(first: Payment, paid: StarPayment) -> bool:
    """Первая оплата, выданная версией, которая не запоминала списание."""
    return (
        first.status == PaymentStatus.PAID.value
        and first.external_id is None
        and first.paid_at is not None
        and paid.paid_at < first.paid_at + _FIRST_PAYMENT_WINDOW
    )
