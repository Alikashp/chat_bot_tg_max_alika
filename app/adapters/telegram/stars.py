"""Оплата звёздами Telegram.

Внешнего провайдера здесь нет вовсе: счёт выставляет сам мессенджер, деньги
списывает он же, и спросить у кого-то статус платежа нельзя. Поэтому у этого
адаптера нет метода «оплачено ли» — единственный источник правды приходит
обновлением ``successful_payment``, и приходит он от того же Telegram, чьи
запросы уже подписаны секретом вебхука.

Валюта `XTR` и пустой `provider_token` — это и есть признак того, что платёж
идёт звёздами, а не через платёжного провайдера.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import LabeledPrice

from app.core import texts
from app.core.models import Chat, MessageRef

#: Валюта звёзд Telegram.
STARS_CURRENCY = "XTR"


class TelegramStars:
    """Реализация порта StarsPayments."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_invoice(
        self,
        chat: Chat,
        *,
        title: str,
        description: str,
        order_id: str,
        stars: int,
    ) -> MessageRef:
        """Выставляет счёт в звёздах.

        ``payload`` — наш идентификатор заказа. Он вернётся и в запросе перед
        списанием, и в подтверждении оплаты; по нему и опознаём, за что
        заплатили.
        """
        message = await self._bot.send_invoice(
            chat_id=chat.chat_id,
            title=title,
            description=description,
            payload=order_id,
            currency=STARS_CURRENCY,
            prices=[LabeledPrice(label=title, amount=stars)],
        )
        return MessageRef(chat=chat, message_id=str(message.message_id))

    async def approve(
        self, request_id: str, *, ok: bool, reason: str | None = None
    ) -> None:
        """Отвечает на запрос перед списанием.

        Telegram ждёт ответа считаные секунды и без него платёж не проведёт.
        Текст отказа обязателен, когда отказываем: пользователь увидит именно
        его.
        """
        await self._bot.answer_pre_checkout_query(
            pre_checkout_query_id=request_id,
            ok=ok,
            error_message=None if ok else (reason or texts.PAYMENT_REFUSED),
        )
