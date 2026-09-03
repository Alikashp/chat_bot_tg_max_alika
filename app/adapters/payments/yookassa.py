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
from app.core.tariffs import RUB
from app.ports.payments import PaymentIntent

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
        recurring: bool = False,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._return_url = return_url
        # Автоплатежи у ЮKassa подключаются отдельно и не всякому магазину.
        # Пока их не включили, повторных списаний не будет, и обещать
        # продление нельзя: подписка на карте окажется разовой оплатой.
        # Поэтому это настройка, а не константа, — и по ней же сценарий
        # решает, что писать на экране заказа.
        self.recurring = recurring
        credentials = b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
        }

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_rub: int,
        description: str,
        save_method: bool = False,
    ) -> PaymentIntent:
        """Создаёт платёж и возвращает ссылку на оплату.

        Ключ идемпотентности — наш идентификатор заказа. Благодаря ему второе
        нажатие кнопки не создаёт второй платёж: ЮKassa вернёт тот же самый.

        ``save_method`` просит ЮKassa запомнить карту, чтобы списывать по ней
        дальше. Реквизиты остаются у неё: нам вернётся только идентификатор
        способа оплаты.
        """
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount_rub}.00", "currency": RUB},
            # Списываем сразу, без двухстадийности: подписка выдаётся тут же,
            # и держать деньги в холде не за чем.
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self._return_url},
            "description": description,
            # Свой идентификатор кладём в метаданные, чтобы узнать заказ по
            # уведомлению, не заводя отдельной таблицы соответствий.
            "metadata": {"order_id": order_id},
        }
        if save_method:
            payload["save_payment_method"] = True

        response = await self._create(payload, idempotence_key=order_id)

        confirmation = response.get("confirmation")
        url = (
            confirmation.get("confirmation_url")
            if isinstance(confirmation, dict)
            else None
        )
        return PaymentIntent(
            external_id=_payment_id(response),
            confirmation_url=url if isinstance(url, str) and url else None,
        )

    async def charge_saved(
        self,
        *,
        order_id: str,
        amount_rub: int,
        description: str,
        payment_method_id: str,
    ) -> str | None:
        """Списывает по сохранённой карте. Возвращает платёж или None.

        None — это отказ, а не сбой: на карте не хватило денег, истёк срок,
        банк не пропустил. Такое разбирается не исключением, а §4.16 оферты —
        повторами в течение трёх дней. Исключение остаётся за случаем, когда
        ЮKassa не ответила вовсе: тогда неизвестно, списали или нет, и
        считать попытку неудачной нельзя.

        Идемпотентность та же, что и у первого платежа: наш заказ. Повторный
        вызов по тому же заказу не спишет второй раз.
        """
        response = await self._create(
            {
                "amount": {"value": f"{amount_rub}.00", "currency": RUB},
                "capture": True,
                "description": description,
                "payment_method_id": payment_method_id,
                "metadata": {"order_id": order_id},
            },
            idempotence_key=order_id,
        )
        if response.get("status") != _SUCCEEDED or response.get("paid") is not True:
            return None
        return _payment_id(response)

    async def saved_method_of(self, external_id: str) -> str | None:
        """Идентификатор карты, сохранённой при этом платеже.

        Появляется только после успешной оплаты и только если её просили
        сохранить. Пусто — значит списывать в следующий раз будет нечем, и
        подписку заводить не на чем.
        """
        response = await self._payment(external_id)
        method = response.get("payment_method")
        if not isinstance(method, dict) or method.get("saved") is not True:
            return None
        method_id = method.get("id")
        return method_id if isinstance(method_id, str) and method_id else None

    async def _create(
        self, payload: dict[str, Any], *, idempotence_key: str
    ) -> dict[str, Any]:
        """Создаёт платёж. Общее для первой оплаты и для продления."""
        return await request_json(
            self._client,
            "POST",
            f"{self._base_url}/payments",
            headers={**self._headers, "Idempotence-Key": idempotence_key},
            json=payload,
        )

    async def _payment(self, external_id: str) -> dict[str, Any]:
        """Читает платёж у провайдера — нашим ключом, а не по уведомлению."""
        return await request_json(
            self._client,
            "GET",
            f"{self._base_url}/payments/{external_id}",
            headers=self._headers,
        )

    async def is_paid(self, external_id: str, *, expected_rub: int) -> bool:
        """Спрашивает у ЮKassa, оплачен ли платёж на нужную сумму.

        Сумму сверяем не из недоверия к провайдеру, а из недоверия к
        уведомлению: оно называет идентификатор платежа, и без проверки суммы
        оплата дешёвого тарифа закрывала бы заказ на дорогой.
        """
        response = await self._payment(external_id)
        if response.get("status") != _SUCCEEDED or response.get("paid") is not True:
            return False

        amount = response.get("amount")
        if not isinstance(amount, dict):
            return False
        return _rubles(amount.get("value")) == expected_rub and (
            amount.get("currency") == RUB
        )


def _payment_id(response: dict[str, Any]) -> str:
    """Идентификатор платежа из ответа. Без него платёж для нас не существует."""
    payment_id = response.get("id")
    if not isinstance(payment_id, str) or not payment_id:
        raise ProviderError("ЮKassa не вернула идентификатор платежа")
    return payment_id


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
