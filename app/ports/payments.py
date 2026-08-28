"""Порт платежей.

Реализуется на фазе 8 двумя адаптерами: ЮKassa (карта) и Telegram Stars.
Порт объявлен заранее по прямому требованию §2.8 задания — платёжный слой
должен быть спроектирован как порт с двумя адаптерами ещё до того, как
появится реальная оплата. Заглушка «Скоро» живёт в адаптере, не в ядре.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.models import TariffId, UserId


class PaymentMethod(StrEnum):
    """Способ оплаты (§2.8)."""

    CARD = "card"
    STARS = "stars"


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """Начатый платёж.

    ``confirmation_url`` — куда отправить пользователя (ЮKassa).
    ``invoice_payload`` — что показать средствами мессенджера (Stars).
    Заполнен ровно один из двух.
    """

    payment_id: str
    method: PaymentMethod
    tariff: TariffId
    amount: int
    currency: str
    confirmation_url: str | None = None
    invoice_payload: str | None = None


class PaymentProvider(Protocol):
    """Провайдер оплаты подписки."""

    method: PaymentMethod

    async def create_payment(
        self, user_id: UserId, tariff: TariffId, *, idempotency_key: str
    ) -> PaymentIntent:
        """Начинает оплату тарифа.

        ``idempotency_key`` обязателен: повторное нажатие кнопки не должно
        создавать второй платёж.
        """
        ...
