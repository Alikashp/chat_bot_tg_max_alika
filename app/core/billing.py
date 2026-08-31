"""Регулярный обход подписок: кого предупредить и с кого списать.

Отдельный проход, а не реакция на сообщение, потому что всё здесь происходит
без человека. Он не пишет боту в день списания — списание случается само, и
предупредить о нём тоже должны мы сами (§4.13 и §4.17 оферты).

Порядок трёх проходов не случаен и читается как обещание, данное в оферте:

1. сначала о новой цене — за неделю (§4.17);
2. потом о самом списании — за сутки (§4.13);
3. и только потом деньги.

Обратный порядок означал бы предупреждение вдогонку списанию, то есть не
предупреждение вовсе.

Зависимости приходят по мессенджерам: подписка живёт в базе, а человек — в
Telegram или в MAX, и отвечать надо туда, откуда он пришёл. Мессенджер
берётся из самого пользователя, а не из того, кто первым оказался в словаре.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from app.core.models import MessengerKind, Subscription, User
from app.core.scenarios import subscriptions
from app.core.scenarios.deps import Deps

#: Шаг обхода: что сделать с одной подпиской.
Step = Callable[[Deps, Subscription], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Billing:
    """Один проход по подпискам, которым что-то причитается."""

    #: Зависимости по мессенджерам. Пустым быть не может: без единого
    #: мессенджера некому и отвечать.
    by_messenger: Mapping[MessengerKind, Deps]
    #: Сколько подписок разбирать за проход. Остаток дождётся следующего.
    batch: int = 100

    def __post_init__(self) -> None:
        if not self.by_messenger:
            raise ValueError("нужен хотя бы один мессенджер")

    @property
    def deps(self) -> Deps:
        """Любые зависимости — для того, что от мессенджера не зависит.

        Часы, настройки и хранилище у всех мессенджеров одни и те же: их
        собирают из одного места. Различается только сам мессенджер, и
        именно его мы выбираем по пользователю.
        """
        return next(iter(self.by_messenger.values()))

    async def run(self) -> None:
        """Один полный проход. Сбой на одной подписке не роняет остальные."""
        await self.check_prices()
        await self.remind()
        await self.charge()

    async def check_prices(self) -> None:
        """Предупреждает о новой цене за неделю до списания (§4.17 оферты)."""
        now = self.deps.now()
        due = await self.deps.storage.subscriptions_to_check_price(
            now,
            now + timedelta(days=self.deps.settings.price_notice_days),
            limit=self.batch,
        )
        for subscription in due:
            await self._each(subscription, subscriptions.check_price, "check_price")

    async def remind(self) -> None:
        """Предупреждает о списании за сутки (§4.13 оферты)."""
        now = self.deps.now()
        due = await self.deps.storage.subscriptions_to_remind(
            now,
            now + timedelta(hours=self.deps.settings.reminder_hours),
            limit=self.batch,
        )
        for subscription in due:
            await self._each(subscription, subscriptions.remind, "remind")

    async def charge(self) -> None:
        """Списывает по тем подпискам, у которых наступил срок."""
        due = await self.deps.storage.subscriptions_to_charge(
            self.deps.now(), limit=self.batch
        )
        for subscription in due:
            await self._each(subscription, subscriptions.charge, "charge")

    async def _each(self, subscription: Subscription, step: Step, name: str) -> None:
        """Выполняет шаг для одной подписки, не давая ей уронить остальные.

        Изоляция здесь не перестраховка: проход идёт по чужим деньгам, и
        отвалившийся мессенджер одного человека не должен оставить остальных
        без списания или без предупреждения о нём.
        """
        deps = await self._deps_for(subscription)
        if deps is None:
            return
        try:
            await step(deps, subscription)
        except Exception as error:
            deps.logger.error(
                "billing_step_failed",
                step=name,
                user_id=int(subscription.user_id),
                error=repr(error),
            )

    async def _deps_for(self, subscription: Subscription) -> Deps | None:
        """Зависимости того мессенджера, из которого пришёл человек.

        Если этот мессенджер сейчас выключен, шаг пропускается целиком —
        включая списание. Взять деньги и не суметь об этом сказать хуже, чем
        не взять: человек всё равно не может пользоваться ботом там, где бота
        нет.
        """
        user = await self._user(subscription)
        if user is None:
            return None
        deps = self.by_messenger.get(user.messenger)
        if deps is None:
            self.deps.logger.warning(
                "billing_messenger_disabled", user_id=int(subscription.user_id)
            )
        return deps

    async def _user(self, subscription: Subscription) -> User | None:
        user = await self.deps.storage.get_user_by_id(subscription.user_id)
        if user is None:
            self.deps.logger.error(
                "billing_user_missing", user_id=int(subscription.user_id)
            )
        return user
