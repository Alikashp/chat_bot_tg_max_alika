"""Оплата картой через ЮKassa.

Свой тонкий клиент на httpx по тем же причинам, что и у провайдеров ИИ:
respx мокает транспорт httpx, а собственные повторы SDK конфликтовали бы с
нашими.

Главное решение этого файла — метод ``is_paid``. У ЮKassa вебхук не подписан
ничем: ни секретом, ни подписью тела. Единственная предлагаемая защита —
сверять адрес отправителя, но список адресов меняется, а за прокси Railway
исходный адрес нам и не виден. Поэтому уведомление считается не фактом
оплаты, а поводом переспросить: статус мы читаем сами, своим ключом, по
идентификатору платежа. Подделать такой ответ нельзя, не имея нашего ключа.
"""

from __future__ import annotations

from base64 import b64encode
from typing import Any

import httpx

from app.adapters.ai.errors import ProviderError
from app.adapters.ai.http import request_json
from app.ports.payments import PaymentIntent

#: Боевой адрес API. В конфиге переопределяется на тестовый магазин.
API_URL = "https://api.yookassa.ru/v3"

#: Статус, при котором деньги действительно у нас.
_SUCCEEDED = "succeeded"


class YooKassaPayments:
    """Реализация порта CardPayments."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        shop_id: str,
        secret_key: str,
        return_url: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._return_url = return_url
        credentials = b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

    async def create_payment(
        self, *, order_id: str, amount_rub: int, description: str
    ) -> PaymentIntent:
        """Создаёт платёж и возвращает ссылку на оплату.

        Ключ идемпотентности — наш идентификатор заказа. Благодаря ему второе
        нажатие кнопки не создаёт второй платёж: ЮKassa вернёт тот же самый.
        """
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
            # Списываем сразу, без двухстадийности: подписка выдаётся тут же,
            # и держать деньги в холде не за чем.
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self._return_url},
            "description": description,
            # Свой идентификатор кладём в метаданные, чтобы узнать заказ по
            # уведомлению, не заводя отдельной таблицы соответствий.
            "metadata": {"order_id": order_id},
        }

        response = await request_json(
            self._client,
            "POST",
            f"{self._base_url}/payments",
            headers={**self._headers, "Idempotence-Key": order_id},
            json=payload,
        )

        payment_id = response.get("id")
        if not isinstance(payment_id, str) or not payment_id:
            raise ProviderError("ЮKassa не вернула идентификатор платежа")

        confirmation = response.get("confirmation")
        url = (
            confirmation.get("confirmation_url")
            if isinstance(confirmation, dict)
            else None
        )
        return PaymentIntent(
            external_id=payment_id,
            confirmation_url=url if isinstance(url, str) and url else None,
        )

    async def is_paid(self, external_id: str, *, expected_rub: int) -> bool:
        """Спрашивает у ЮKassa, оплачен ли платёж на нужную сумму.

        Сумму сверяем не из недоверия к провайдеру, а из недоверия к
        уведомлению: оно называет идентификатор платежа, и без проверки суммы
        оплата дешёвого тарифа закрывала бы заказ на дорогой.
        """
        response = await request_json(
            self._client,
            "GET",
            f"{self._base_url}/payments/{external_id}",
            headers=self._headers,
        )
        if response.get("status") != _SUCCEEDED or response.get("paid") is not True:
            return False

        amount = response.get("amount")
        if not isinstance(amount, dict):
            return False
        return _rubles(amount.get("value")) == expected_rub and (
            amount.get("currency") == "RUB"
        )


def order_id_of(notification: dict[str, Any]) -> str | None:
    """Достаёт наш идентификатор заказа из уведомления ЮKassa.

    Само уведомление ничего не доказывает: это только повод переспросить
    провайдера. Поэтому здесь одно — вытащить, о каком заказе речь.
    """
    payment = notification.get("object")
    if not isinstance(payment, dict):
        return None
    metadata = payment.get("metadata")
    if not isinstance(metadata, dict):
        return None
    order_id = metadata.get("order_id")
    return order_id if isinstance(order_id, str) and order_id else None


def _rubles(value: Any) -> int | None:
    """«299.00» → 299. None, если это не сумма.

    Копейки отбрасываем: цены у нас целые в рублях, и сравниваем с ними.
    """
    if not isinstance(value, str):
        return None
    whole, _, _fraction = value.partition(".")
    try:
        return int(whole)
    except ValueError:
        return None
