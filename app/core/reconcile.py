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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from app.core.generations import error_code
from app.core.models import MessengerKind, Payment
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, session_for


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
        """Один проход. Сбой на одном заказе не мешает остальным."""
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
        subscription = await deps.storage.get_subscription(order.user_id)
        renewal = subscription is not None and subscription.charge_order_id == order.id
        confirmed = await payments.confirm(deps, order.id)
        if confirmed is None:
            return
        deps.logger.info(
            "payment_reconciled", user_id=int(user.id), payment_id=order.id
        )
        await payments.announce(
            deps, session_for(deps, user), confirmed, renewal=renewal
        )
