"""Подписка на звёздах Telegram.

Внешнего провайдера здесь нет вовсе: счёт выставляет сам мессенджер, деньги
списывает он же — и списывает каждый период сам, без нашего участия. Спросить
у кого-то статус платежа нельзя, поэтому у этого адаптера нет метода
«оплачено ли»: единственный источник правды приходит обновлением
``successful_payment``, и приходит он от того же Telegram, чьи запросы уже
подписаны секретом вебхука.

Валюта `XTR` и пустой `provider_token` — это и есть признак того, что платёж
идёт звёздами, а не через платёжного провайдера.

Про ссылку вместо сообщения. Счёт с подпиской Telegram умеет создавать только
через ``createInvoiceLink``: у ``sendInvoice`` параметра ``subscription_period``
нет. Разница не косметическая — без него списание было бы разовым, а мы
обещали продление. Поэтому адаптер возвращает ссылку, а кнопку с ней рисует
сценарий: там же, где показаны условия, под которыми человек её нажимает.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import LabeledPrice

from app.core import texts
from app.core.tariffs import STARS

#: Валюта звёзд Telegram.
STARS_CURRENCY = STARS

#: Единственная длина периода, которую Telegram допускает у звёздной подписки
#: (Bot API: ``subscription_period`` может быть только 2592000 секунд).
#: Проверяем явно: с любым другим числом Telegram ответит ошибкой, и лучше
#: она будет понятной нам, чем «Bad Request» в логе.
SUBSCRIPTION_PERIOD_SECONDS = 30 * 24 * 60 * 60


class TelegramStars:
    """Реализация порта StarsPayments."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def subscription_link(
        self,
        *,
        title: str,
        description: str,
        order_id: str,
        stars: int,
        period_days: int,
    ) -> str:
        """Создаёт счёт на подписку и возвращает ссылку на оплату.

        ``payload`` — наш идентификатор заказа. Он вернётся и в запросе перед
        списанием, и в подтверждении оплаты, и в каждом продлении; по нему и
        опознаём, за что заплатили.
        """
        period = period_days * 24 * 60 * 60
        if period != SUBSCRIPTION_PERIOD_SECONDS:
            raise ValueError(
                "Telegram допускает звёздную подписку только на 30 дней, "
                f"а период задан в {period_days}"
            )

        return await self._bot.create_invoice_link(
            title=title,
            description=description,
            payload=order_id,
            currency=STARS_CURRENCY,
            prices=[LabeledPrice(label=title, amount=stars)],
            subscription_period=period,
        )

    async def cancel(self, *, user_id: str, charge_id: str) -> None:
        """Отменяет подписку на стороне Telegram.

        Без этого вызова Telegram продолжит списывать звёзды, даже если у нас
        подписка отмечена отменённой, — и человек будет прав, требуя возврат.
        """
        await self._bot.edit_user_star_subscription(
            user_id=int(user_id),
            telegram_payment_charge_id=charge_id,
            is_canceled=True,
        )

    async def approve(
        self, request_id: str, *, ok: bool, reason: str | None = None
    ) -> None:
        """Отвечает на запрос перед списанием.

        Telegram спрашивает об этом и перед первой оплатой, и перед каждым
        продлением, ждёт ответа считаные секунды и без него платёж не
        проведёт. Текст отказа обязателен, когда отказываем: пользователь
        увидит именно его.
        """
        await self._bot.answer_pre_checkout_query(
            pre_checkout_query_id=request_id,
            ok=ok,
            error_message=None if ok else (reason or texts.PAYMENT_REFUSED),
        )
