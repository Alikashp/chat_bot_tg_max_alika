"""Сборка: какой магазин ЮKassa достаётся какому мессенджеру.

ЮKassa регистрирует приложения по отдельности, и у бота в MAX собственные
идентификатор магазина и ключ. Ошибка здесь не падает и не видна в логах: она
выглядит как исправная работа, а деньги уходят в чужой магазин или платёж
перестаёт подтверждаться. Поэтому проверяется не «настроилось», а куда именно
уходит запрос и чьим ключом он подписан.
"""

from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from typing import Any

import httpx
import respx

from app.config import Settings
from app.core.models import MessengerKind, TariffId
from app.core.scenarios.deps import Deps
from app.main import build_cards
from app.ports.payments import PaymentMethod
from tests.fakes import FakeCards

BASE = "https://api.example/v3"

ENV: dict[str, Any] = {
    "public_url": "https://bot.example.com",
    "telegram_bot_token": "123456:token",
    "telegram_webhook_secret": "a" * 32,
    "database_url": "postgresql://bot:bot@localhost:5432/botdb",
    "llm_api_key": "sk-test",
    "yookassa_base_url": BASE,
    "yookassa_shop_id": "tg-shop",
    "yookassa_secret_key": "tg-key",
    "max_bot_token": "max-token",
    "max_yookassa_shop_id": "max-shop",
    "max_yookassa_secret_key": "max-key",
}


def _settings(**overrides: Any) -> Settings:
    return Settings.model_validate({**ENV, **overrides})


def _authorization(shop_id: str, secret: str) -> str:
    return "Basic " + b64encode(f"{shop_id}:{secret}".encode()).decode()


# --- Какой магазин достаётся какому мессенджеру --------------------------


@respx.mock
async def test_each_messenger_pays_into_its_own_shop() -> None:
    """Ключ, которым подписан запрос, и есть ответ на вопрос «чьи деньги».

    Проверяем именно заголовок, а не настройки: перепутать магазины можно и
    при верных переменных, если провайдер собран не из тех.
    """
    route = respx.post(f"{BASE}/payments").mock(
        return_value=httpx.Response(200, json={"id": "2d0a1b"})
    )
    settings = _settings()

    telegram, _ = build_cards(
        settings, messenger=MessengerKind.TELEGRAM, return_url="https://t.me/bot"
    )
    inside_max, _ = build_cards(
        settings, messenger=MessengerKind.MAX, return_url="https://max.ru/bot"
    )
    assert telegram is not None and inside_max is not None

    await telegram.create_payment(order_id="o1", amount_rub=599, description="Про")
    await inside_max.create_payment(order_id="o2", amount_rub=599, description="Про")

    assert route.calls[0].request.headers["Authorization"] == _authorization(
        "tg-shop", "tg-key"
    )
    assert route.calls[1].request.headers["Authorization"] == _authorization(
        "max-shop", "max-key"
    )


async def test_max_without_its_own_shop_has_no_card_payments() -> None:
    """Подставить телеграмный магазин нельзя: это деньги в чужой магазин.

    Честнее показать «оплата скоро», чем взять оплату не туда и разбирать
    потом возвратами.
    """
    settings = _settings(max_yookassa_shop_id="", max_yookassa_secret_key="")

    provider, client = build_cards(
        settings, messenger=MessengerKind.MAX, return_url="https://max.ru/bot"
    )

    assert provider is None
    assert client is None
    assert settings.cards_enabled is True


async def test_the_telegram_shop_stands_on_its_own_too() -> None:
    """Обратная сторона: MAX настроен, Telegram — нет."""
    settings = _settings(yookassa_shop_id="", yookassa_secret_key="")

    provider, _ = build_cards(
        settings, messenger=MessengerKind.TELEGRAM, return_url="https://t.me/bot"
    )

    assert provider is None
    assert settings.max_cards_enabled is True


async def test_recurring_is_decided_per_shop() -> None:
    """Автоплатежи подключают каждому магазину отдельно и в разное время."""
    settings = _settings(yookassa_recurring=True, max_yookassa_recurring=False)

    telegram, _ = build_cards(
        settings, messenger=MessengerKind.TELEGRAM, return_url="https://t.me/bot"
    )
    inside_max, _ = build_cards(
        settings, messenger=MessengerKind.MAX, return_url="https://max.ru/bot"
    )

    assert telegram is not None and inside_max is not None
    assert telegram.recurring is True
    assert inside_max.recurring is False


# --- Уведомление об оплате -----------------------------------------------


async def _order(deps: Deps, messenger: MessengerKind, external_id: str) -> str:
    from app.core import support

    user = await deps.storage.create_user(
        messenger=messenger,
        external_id=f"{messenger.value}-1",
        referral_code=f"code-{messenger.value}",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )
    order = await deps.storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method=PaymentMethod.CARD.value,
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    await deps.storage.attach_external_id(order.id, external_id)
    return order.id


async def test_the_notice_is_checked_against_the_shop_that_was_paid(
    deps: Deps,
) -> None:
    """Спросить чужим ключом — получить «нет такого» и не выдать тариф.

    Магазины разные, а адрес вебхука один: какой из них спрашивать, решает
    сам плательщик, точнее — мессенджер, из которого он пришёл.
    """
    from app.main import _build_settlement

    # Телеграмный магазин про этот платёж не знает — так и отвечает.
    telegram_cards = FakeCards()
    telegram_cards.paid = False
    max_cards = FakeCards()
    telegram = replace(deps, cards=telegram_cards)
    inside_max = replace(deps, cards=max_cards)
    order_id = await _order(inside_max, MessengerKind.MAX, "yk-max-1")

    settlement = _build_settlement(
        {MessengerKind.TELEGRAM: telegram, MessengerKind.MAX: inside_max}
    )
    await settlement.handle(
        {"object": {"id": "yk-max-1", "metadata": {"order_id": order_id}}}
    )

    assert max_cards.asked == ["yk-max-1"]
    assert telegram_cards.asked == []
    order = await deps.storage.get_payment(order_id)
    assert order is not None
    user = await deps.storage.get_user_by_id(order.user_id)
    assert user is not None
    assert user.tariff is TariffId.PRO


async def test_a_notice_we_cannot_verify_grants_nothing(deps: Deps) -> None:
    """Мессенджер выключен — ключа от его магазина у нас нет.

    Уведомление само по себе ничего не доказывает: оно не подписано. Выдать
    по нему тариф значило бы раздавать подписки любому, кто узнал адрес.
    """
    from app.main import _build_settlement

    telegram = replace(deps, cards=FakeCards())
    order_id = await _order(telegram, MessengerKind.MAX, "yk-max-2")

    settlement = _build_settlement({MessengerKind.TELEGRAM: telegram})
    await settlement.handle(
        {"object": {"id": "yk-max-2", "metadata": {"order_id": order_id}}}
    )

    order = await deps.storage.get_payment(order_id)
    assert order is not None
    user = await deps.storage.get_user_by_id(order.user_id)
    assert user is not None
    assert user.tariff is TariffId.FREE
