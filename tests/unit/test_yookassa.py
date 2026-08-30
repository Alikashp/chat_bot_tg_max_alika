"""Адаптер ЮKassa. Главное здесь — что уведомлению не верят на слово.

У ЮKassa вебхук не подписан ничем: ни секретом, ни подписью тела. Значит
любой, кто узнал адрес, может прислать «оплачено». Единственная защита —
переспросить провайдера по нашему ключу, и эти тесты проверяют именно её.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.adapters.ai.errors import ProviderRequestError
from app.adapters.payments.yookassa import YooKassaPayments, order_id_of

BASE = "https://api.example/v3"
PAYMENTS_URL = f"{BASE}/payments"


def _provider() -> YooKassaPayments:
    return YooKassaPayments(
        httpx.AsyncClient(),
        base_url=BASE,
        shop_id="123456",
        secret_key="test_secret",
        return_url="https://t.me/testbot",
    )


def _created(payment_id: str = "2d0a1b") -> dict[str, object]:
    return {
        "id": payment_id,
        "status": "pending",
        "confirmation": {
            "type": "redirect",
            "confirmation_url": "https://yoomoney.ru/checkout/2d0a1b",
        },
    }


def _succeeded(value: str = "599.00", currency: str = "RUB") -> dict[str, object]:
    return {
        "id": "2d0a1b",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": value, "currency": currency},
    }


# --- Создание платежа ----------------------------------------------------


@respx.mock
async def test_a_payment_returns_a_link() -> None:
    respx.post(PAYMENTS_URL).mock(return_value=httpx.Response(200, json=_created()))

    intent = await _provider().create_payment(
        order_id="order-1", amount_rub=599, description="Тариф Про"
    )

    assert intent.external_id == "2d0a1b"
    assert intent.confirmation_url == "https://yoomoney.ru/checkout/2d0a1b"


@respx.mock
async def test_the_order_is_the_idempotency_key() -> None:
    """Второе нажатие кнопки не должно создавать второй платёж."""
    route = respx.post(PAYMENTS_URL).mock(
        return_value=httpx.Response(200, json=_created())
    )

    await _provider().create_payment(
        order_id="order-1", amount_rub=599, description="Тариф Про"
    )

    assert route.calls.last.request.headers["Idempotence-Key"] == "order-1"


@respx.mock
async def test_the_order_travels_in_the_metadata() -> None:
    """По нему потом опознаётся уведомление — своей таблицы не нужно."""
    route = respx.post(PAYMENTS_URL).mock(
        return_value=httpx.Response(200, json=_created())
    )

    await _provider().create_payment(
        order_id="order-1", amount_rub=599, description="Тариф Про"
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["metadata"] == {"order_id": "order-1"}
    assert payload["amount"] == {"value": "599.00", "currency": "RUB"}
    assert payload["capture"] is True


@respx.mock
async def test_the_shop_credentials_are_sent() -> None:
    route = respx.post(PAYMENTS_URL).mock(
        return_value=httpx.Response(200, json=_created())
    )

    await _provider().create_payment(
        order_id="order-1", amount_rub=599, description="Тариф Про"
    )

    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_a_rejected_request_is_reported() -> None:
    respx.post(PAYMENTS_URL).mock(
        return_value=httpx.Response(400, json={"code": "invalid_request"})
    )

    with pytest.raises(ProviderRequestError):
        await _provider().create_payment(
            order_id="order-1", amount_rub=599, description="Тариф Про"
        )


# --- Проверка оплаты -----------------------------------------------------


@respx.mock
async def test_a_succeeded_payment_is_confirmed() -> None:
    respx.get(f"{PAYMENTS_URL}/2d0a1b").mock(
        return_value=httpx.Response(200, json=_succeeded())
    )

    assert await _provider().is_paid("2d0a1b", expected_rub=599) is True


@respx.mock
async def test_a_pending_payment_is_not_confirmed() -> None:
    """Уведомление могло прийти раньше денег — или не от ЮKassa вовсе."""
    respx.get(f"{PAYMENTS_URL}/2d0a1b").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "2d0a1b",
                "status": "pending",
                "paid": False,
                "amount": {"value": "599.00", "currency": "RUB"},
            },
        )
    )

    assert await _provider().is_paid("2d0a1b", expected_rub=599) is False


@respx.mock
async def test_a_smaller_payment_does_not_close_a_bigger_order() -> None:
    """Иначе оплата дешёвого тарифа включала бы дорогой."""
    respx.get(f"{PAYMENTS_URL}/2d0a1b").mock(
        return_value=httpx.Response(200, json=_succeeded(value="299.00"))
    )

    assert await _provider().is_paid("2d0a1b", expected_rub=599) is False


@respx.mock
async def test_another_currency_does_not_count() -> None:
    respx.get(f"{PAYMENTS_URL}/2d0a1b").mock(
        return_value=httpx.Response(200, json=_succeeded(currency="USD"))
    )

    assert await _provider().is_paid("2d0a1b", expected_rub=599) is False


# --- Разбор уведомления --------------------------------------------------


def test_the_order_is_read_from_the_notification() -> None:
    notification = {
        "type": "notification",
        "event": "payment.succeeded",
        "object": {"id": "2d0a1b", "metadata": {"order_id": "order-1"}},
    }

    assert order_id_of(notification) == "order-1"


@pytest.mark.parametrize(
    "notification",
    [
        {},
        {"object": None},
        {"object": {"id": "2d0a1b"}},
        {"object": {"metadata": {}}},
        {"object": {"metadata": {"order_id": ""}}},
    ],
)
def test_a_notification_without_an_order_is_ignored(
    notification: dict[str, object],
) -> None:
    """Уведомление приходит от кого угодно: разбирать надо осторожно."""
    assert order_id_of(notification) is None
