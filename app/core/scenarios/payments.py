"""Оплата подписки (§2.8).

Здесь живёт единственное правило, ради которого весь этот слой существует:

    тариф выдаётся только по подтверждённой оплате, и ровно один раз.

Оба слова важны. «Подтверждённой» — потому что уведомлению об оплате верить
нельзя: у ЮKassa вебхук не подписан ничем, и любой, кто узнал адрес, мог бы
выдавать себе подписки. Поэтому уведомление здесь считается не фактом, а лишь
поводом переспросить провайдера по нашему ключу.

«Ровно один раз» — потому что уведомления приходят по несколько штук, а
подписка продлевается на месяц. Защита не в проверке «а не выдавали ли уже», а
в атомарном переходе заказа в «оплачен»: выигрывает ровно одно уведомление,
остальные получают False и уходят ни с чем.

Симметрия с главным инвариантом проекта неслучайна. Там мы не списываем лимит,
пока результат не доставлен. Здесь мы не выдаём тариф, пока деньги не
подтверждены. В обе стороны ошибка стоит доверия.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from app.core import texts
from app.core.actions import method_action
from app.core.limits import current_day
from app.core.models import Button, Keyboard, Payment, TariffId, User
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import stars_price, tariff_of
from app.ports.payments import PaymentMethod, PaymentStatus

#: Валюты платежей. Звёзды — не деньги в обычном смысле, но учёт нужен и им.
_RUB = "RUB"
_STARS = "XTR"


async def choose_method(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Спрашивает, чем платить.

    Способы перечисляются по тому, какие вообще есть: карта настраивается
    ключами провайдера, звёзды бывают только в Telegram. Если не настроено
    ничего — честная заглушка вместо выбора из пустого списка.
    """
    stars = _stars_price(deps, tariff_id) if deps.stars is not None else None
    if deps.cards is None and stars is None:
        await _payments_not_ready(deps, session)
        return

    if deps.cards is None:
        # Единственный способ — не выбор. Не заставляем нажимать лишний раз.
        await start_stars(deps, session, tariff_id)
        return

    screen = texts.payment_methods(
        tariff_id, days=deps.settings.subscription_days, stars=stars
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=_method_keyboard(tariff_id, stars=stars)
    )


async def start_card(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Заводит заказ и отдаёт ссылку на оплату."""
    if deps.cards is None:
        await _payments_not_ready(deps, session)
        return

    tariff = tariff_of(tariff_id)
    order = await deps.storage.create_payment(
        user_id=session.user.id,
        tariff=tariff_id,
        method=PaymentMethod.CARD.value,
        amount=tariff.price_rub,
        currency=_RUB,
    )

    try:
        intent = await deps.cards.create_payment(
            order_id=order.id,
            amount_rub=tariff.price_rub,
            description=texts.invoice(tariff_id, days=deps.settings.subscription_days)[
                0
            ],
        )
    except Exception as error:
        # Заказ остаётся в pending и просто протухнет. Денег с человека при
        # этом не взяли, поэтому и отменять нечего.
        deps.logger.warning(
            "payment_start_failed",
            user_id=int(session.user.id),
            method=PaymentMethod.CARD.value,
            error=repr(error),
        )
        await _say(deps, session, texts.payment_failed().text)
        return

    if not await deps.storage.attach_external_id(order.id, intent.external_id):
        # Платёж уже привязан к другому заказу. Ссылку отдавать нельзя:
        # подтвердить по ней оплату мы не сможем, а деньги человек отдаст.
        deps.logger.error("payment_id_taken", user_id=int(session.user.id))
        await _say(deps, session, texts.payment_failed().text)
        return

    if intent.confirmation_url is None:
        deps.logger.error("payment_without_url", user_id=int(session.user.id))
        await _say(deps, session, texts.payment_failed().text)
        return

    screen = texts.payment_link()
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=Keyboard.row(
            Button(text=texts.BUTTON_PAY_OPEN, url=intent.confirmation_url)
        ),
        show_menu=False,
    )


async def start_stars(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Заводит заказ и выставляет счёт средствами мессенджера."""
    if deps.stars is None:
        await _payments_not_ready(deps, session)
        return

    stars = _stars_price(deps, tariff_id)
    order = await deps.storage.create_payment(
        user_id=session.user.id,
        tariff=tariff_id,
        method=PaymentMethod.STARS.value,
        amount=stars,
        currency=_STARS,
    )

    title, description = texts.invoice(tariff_id, days=deps.settings.subscription_days)
    try:
        await deps.stars.send_invoice(
            session.chat,
            title=title,
            description=description,
            order_id=order.id,
            stars=stars,
        )
    except Exception as error:
        deps.logger.warning(
            "payment_start_failed",
            user_id=int(session.user.id),
            method=PaymentMethod.STARS.value,
            error=repr(error),
        )
        await _say(deps, session, texts.payment_failed().text)


async def approve(
    deps: Deps, request_id: str, order_id: str, *, user: User | None
) -> None:
    """Отвечает мессенджеру, готовы ли принять оплату.

    Соглашаться на платёж, которого мы не заводили, нельзя: деньги спишутся, а
    выдать по ним будет нечего, и разбираться придётся возвратом. Отказ здесь
    стоит человеку одной неудачной попытки, согласие вслепую — наших денег и
    его доверия.

    Пользователь может быть и неизвестен: запрос приходит от мессенджера, а не
    из переписки. Заводить человека на таком событии незачем — счёта у него
    всё равно нет, — но и молчать нельзя: без ответа платёж повиснет.
    """
    if deps.stars is None:
        return

    order = await deps.storage.get_payment(order_id) if order_id else None
    known = (
        order is not None
        and user is not None
        and order.user_id == user.id
        and order.status == PaymentStatus.PENDING.value
    )
    if not known:
        deps.logger.warning(
            "payment_unknown_order",
            user_id=int(user.id) if user is not None else None,
        )
    await deps.stars.approve(request_id, ok=known)


async def confirm(deps: Deps, order_id: str) -> Payment | None:
    """Отмечает заказ оплаченным и выдаёт тариф. Возвращает заказ, если выдали.

    Сессии здесь нет намеренно: подтверждение приходит и вебхуком ЮKassa, где
    никакого «текущего пользователя» не существует. Пользователь берётся из
    самого заказа.
    """
    order = await deps.storage.get_payment(order_id)
    if order is None:
        deps.logger.warning("payment_confirm_unknown")
        return None

    if not await deps.storage.mark_paid(order.id):
        # Уведомления об оплате приходят по несколько раз. Это не ошибка —
        # просто продлевать подписку на каждое нельзя.
        deps.logger.info("payment_already_confirmed", user_id=int(order.user_id))
        return None

    user = await deps.storage.get_user_by_id(order.user_id)
    if user is None:
        deps.logger.error("payment_user_missing", user_id=int(order.user_id))
        return None

    expires_at = _new_expiry(
        deps,
        current=user.tariff_expires_at,
        bought=order.tariff,
        current_tariff=user.tariff,
    )
    await deps.storage.set_tariff(order.user_id, order.tariff, expires_at)
    deps.logger.info(
        "payment_confirmed",
        user_id=int(order.user_id),
        tariff=order.tariff.value,
        method=order.method,
    )
    return replace(order, paid_at=deps.now())


async def announce(deps: Deps, session: Session, order: Payment) -> None:
    """Говорит человеку, что тариф включён.

    Дату берём из пользователя, а не считаем заново: показать надо ровно тот
    срок, который записан, иначе экран и база разойдутся.
    """
    user = await deps.storage.get_user_by_id(order.user_id)
    until = user.tariff_expires_at if user is not None else None
    day = (
        current_day(until, deps.settings.timezone)
        if until is not None
        else deps.today()
    )
    screen = texts.payment_done(order.tariff, until=texts.format_date(day))
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)


# --- Вспомогательное -----------------------------------------------------


def _new_expiry(
    deps: Deps,
    *,
    current: datetime | None,
    bought: TariffId,
    current_tariff: TariffId,
) -> datetime:
    """До какого момента действует подписка после оплаты.

    Продлеваем от старого срока, а не от сегодня: иначе человек, оплативший
    заранее, терял бы остаток. Но только если тариф тот же — при переходе на
    другой остаток чужого тарифа считать не во что.
    """
    days = timedelta(days=deps.settings.subscription_days)
    now = deps.now()
    if current is not None and current > now and bought is current_tariff:
        return current + days
    return now + days


def _stars_price(deps: Deps, tariff_id: TariffId) -> int:
    return stars_price(
        tariff_of(tariff_id),
        markup=deps.settings.stars_markup,
        rub_per_star=deps.settings.rub_per_star,
    )


def _method_keyboard(tariff_id: TariffId, *, stars: int | None) -> Keyboard:
    buttons = [
        Button(
            text=texts.BUTTON_PAY_CARD,
            action=method_action(PaymentMethod.CARD.value, tariff_id.value),
        )
    ]
    if stars is not None:
        buttons.append(
            Button(
                text=texts.BUTTON_PAY_STARS,
                action=method_action(PaymentMethod.STARS.value, tariff_id.value),
            )
        )
    return Keyboard.row(*buttons)


async def _payments_not_ready(deps: Deps, session: Session) -> None:
    """Оплата не настроена. Тупика быть не должно и здесь."""
    screen = texts.payments_soon()
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.payments_soon()
    )


async def _say(deps: Deps, session: Session, text: str) -> None:
    await deps.messenger.send_text(session.chat, text, show_menu=True)
