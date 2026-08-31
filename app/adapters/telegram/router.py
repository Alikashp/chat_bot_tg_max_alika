"""Приведение обновлений Telegram к общему виду и передача их в ядро.

Разбор и только разбор: что делать с разобранным, решает app/core/router.py —
общий для обоих мессенджеров. Граница проходит ровно здесь, и на фазе 7
MAX-адаптеру достаточно будет собрать такой же IncomingMessage.
"""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from app.core import router, texts
from app.core.models import Chat, IncomingMessage, MessengerKind
from app.core.scenarios.deps import Deps
from app.infra.logging import get_logger

logger = get_logger(__name__)

#: Бот личный: в группах он не нужен и молчит там намеренно. Обрабатывать
#: групповые сообщения значило бы отвечать на каждую реплику в чужой беседе.
_PRIVATE = "private"


def build_dispatcher(deps: Deps) -> Dispatcher:
    """Собирает диспетчер aiogram поверх общего маршрутизатора."""
    dispatcher = Dispatcher()

    @dispatcher.message()
    async def on_message(message: Message) -> None:
        incoming = to_incoming(message)
        if incoming is not None:
            await _dispatch(deps, incoming)

    @dispatcher.callback_query()
    async def on_callback(callback: CallbackQuery) -> None:
        incoming = callback_to_incoming(callback)
        if incoming is not None:
            await _dispatch(deps, incoming)

    @dispatcher.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery) -> None:
        """Telegram спрашивает перед списанием и ждёт ответа секунды."""
        await _dispatch(deps, pre_checkout_to_incoming(query))

    return dispatcher


async def _dispatch(deps: Deps, incoming: IncomingMessage) -> None:
    """Зовёт ядро и не даёт неожиданному сбою оставить человека без ответа."""
    try:
        await router.handle(deps, incoming)
    except Exception as error:
        # Содержимое сообщения в лог не попадает — только тип ошибки (§3.5).
        logger.error(
            "update_failed",
            messenger="telegram",
            user_id=_as_int(incoming.external_user_id),
            error=repr(error),
        )
        await _apologise(deps, incoming)


async def _apologise(deps: Deps, incoming: IncomingMessage) -> None:
    """Говорит пользователю, что не вышло.

    Молчание после сбоя выглядит как сломанный бот, и человек уходит. Если не
    отправляется и это — сдаёмся, но уже с записью в логе.
    """
    try:
        await deps.messenger.send_text(
            incoming.chat, texts.internal_error().text, show_menu=True
        )
    except Exception as error:
        logger.error("apology_failed", messenger="telegram", error=repr(error))


def to_incoming(message: Message) -> IncomingMessage | None:
    """Переводит сообщение Telegram в общий вид; None — если оно не для нас."""
    if message.from_user is None or message.from_user.is_bot:
        return None
    if message.chat.type != _PRIVATE:
        return None

    chat = Chat(messenger=MessengerKind.TELEGRAM, chat_id=str(message.chat.id))

    if message.successful_payment is not None:
        # Деньги списаны. Заказ опознаём по payload, который сами же и
        # положили в счёт.
        #
        # Продление приходит тем же payload'ом, что и первая оплата: Telegram
        # ссылается на счёт подписки, а не на конкретный период. Отличает их
        # пара признаков — is_recurring говорит, что это подписка вообще, а
        # is_first_recurring, что это её первый платёж. Продление, стало
        # быть, второе без первого; и только у него идентификатор списания
        # ещё не встречался нам в базе.
        payment = message.successful_payment
        return IncomingMessage(
            chat=chat,
            external_user_id=str(message.from_user.id),
            paid_order_id=payment.invoice_payload,
            paid_renewal=bool(payment.is_recurring)
            and not bool(payment.is_first_recurring),
            paid_charge_id=payment.telegram_payment_charge_id,
        )

    text = message.text or message.caption
    payload = _start_payload(text)

    return IncomingMessage(
        chat=chat,
        external_user_id=str(message.from_user.id),
        # Саму команду в текст не отдаём: иначе «/start» уехал бы в чат
        # вопросом пользователя и стоил бы ему сообщения.
        text=None if payload is not None else text,
        photo_ref=message.photo[-1].file_id if message.photo else None,
        start_payload=payload,
    )


def callback_to_incoming(callback: CallbackQuery) -> IncomingMessage | None:
    """Переводит нажатие inline-кнопки в общий вид."""
    if callback.data is None:
        return None

    # Личный чат — единственный, где бот работает, и там chat_id совпадает с
    # идентификатором пользователя. Поэтому кнопка остаётся рабочей, даже если
    # сообщение под ней уже недоступно боту.
    chat_id = (
        str(callback.message.chat.id)
        if callback.message is not None
        else str(callback.from_user.id)
    )
    chat = Chat(messenger=MessengerKind.TELEGRAM, chat_id=chat_id)

    return IncomingMessage(
        chat=chat,
        external_user_id=str(callback.from_user.id),
        action=callback.data,
        callback_id=callback.id,
    )


def pre_checkout_to_incoming(query: PreCheckoutQuery) -> IncomingMessage:
    """Переводит запрос перед списанием в общий вид.

    Чата у запроса нет: Telegram спрашивает про платёж, а не про переписку.
    Бот личный, поэтому идентификатор чата совпадает с пользователем.
    """
    chat = Chat(messenger=MessengerKind.TELEGRAM, chat_id=str(query.from_user.id))
    return IncomingMessage(
        chat=chat,
        external_user_id=str(query.from_user.id),
        pre_checkout_id=query.id,
        pre_checkout_order_id=query.invoice_payload,
    )


def _start_payload(text: str | None) -> str | None:
    """Достаёт payload из /start; None — если это не команда старта.

    Пустая строка — тоже ответ: значит /start без ссылки. Отличать её от
    «это не /start» обязательно, иначе обычное сообщение начнёт здороваться.
    """
    if text is None or not text.startswith(router.START_COMMAND):
        return None

    head, _, payload = text.partition(" ")
    if head != router.START_COMMAND and not head.startswith(f"{router.START_COMMAND}@"):
        # Другая команда, начинающаяся так же: /startover.
        return None
    return payload.strip()


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
