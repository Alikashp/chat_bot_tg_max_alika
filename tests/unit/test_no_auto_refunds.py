"""Возвраты делает только заказчик, вручную, в кабинете ЮKassa (сессия 14, О5).

Бот возврат только замечает (уведомление ``refund.succeeded``) и отражает его
в заказе. Создать возврат ему нечем — и так должно оставаться.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.adapters.payments import yookassa
from app.ports.payments import CardPayments


def test_the_card_port_cannot_make_a_refund() -> None:
    methods = [name for name, _ in inspect.getmembers(CardPayments)]
    assert [name for name in methods if "refund" in name] == ["is_refunded"]


def test_the_yookassa_adapter_never_calls_the_refunds_api() -> None:
    source = Path(inspect.getfile(yookassa)).read_text()
    assert "/refunds" not in source
    assert "refunds" not in source.replace("refunded", "")
