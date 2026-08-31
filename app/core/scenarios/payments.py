"""Оформление подписки и её оплата (§2.8).

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

Подписка добавляет к этому второе правило, столь же жёсткое: **автопродление
существует только там, где о нём сказали до денег**. Экран заказа и запись
``Subscription`` заводятся из одного и того же решения ``_recurring``, поэтому
разойтись «на экране обещали продление, а его нет» они не могут.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from app.core import texts
from app.core.actions import method_action
from app.core.limits import current_day
from app.core.models import (
    Button,
    Keyboard,
    Payment,
    Subscription,
    TariffId,
    User,
    UserId,
)
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import RUB, STARS, stars_price, tariff_of
from app.ports.payments import PaymentMethod, PaymentStatus, SubscriptionStatus


async def choose_method(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Спрашивает, чем платить, — если способов правда два.

    Когда способ один, выбирать нечего, и лишний экран только отделяет
    человека от условий. Условия при этом не теряются: они на следующем
    экране, ровно над кнопкой оплаты, и туда мы и уходим.
    """
    if not deps.settings.documents_ready:
        # Документы не опубликованы. Брать деньги, не показав условия, нельзя
        # ни юридически, ни по-человечески.
        deps.logger.warning("payment_documents_missing", user_id=int(session.user.id))
        await _payments_not_ready(deps, session)
        return

    cards = deps.cards is not None
    stars = _stars_price(deps, tariff_id) if deps.stars is not None else None

    if cards and stars is not None:
        screen = texts.payment_methods(
            tariff_id, price_rub=tariff_of(tariff_id).price_rub, stars=stars
        )
        await deps.messenger.send_text(
            session.chat, screen.text, keyboard=_method_keyboard(tariff_id)
        )
        return

    if cards:
        await start_card(deps, session, tariff_id)
        return
    if stars is not None:
        await start_stars(deps, session, tariff_id)
        return

    await _payments_not_ready(deps, session)


async def start_card(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Заводит заказ и показывает условия вместе со ссылкой на оплату."""
    if deps.cards is None:
        await _payments_not_ready(deps, session)
        return

    tariff = tariff_of(tariff_id)
    recurring = deps.cards.recurring
    order = await _open_order(
        deps,
        session,
        tariff_id,
        method=PaymentMethod.CARD,
        amount=tariff.price_rub,
        currency=RUB,
    )

    try:
        intent = await deps.cards.create_payment(
            order_id=order.id,
            amount_rub=tariff.price_rub,
            description=texts.invoice(tariff_id, days=deps.settings.subscription_days)[
                0
            ],
            # Сохранять способ оплаты просим только тогда, когда собираемся им
            # пользоваться. Иначе провайдер хранил бы карту человека без
            # причины, а мы обещали бы продление, которого не будет.
            save_method=recurring,
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

    await _show_order(
        deps,
        session,
        tariff_id,
        amount=tariff.price_rub,
        currency=RUB,
        url=intent.confirmation_url,
        recurring=recurring,
    )


async def start_stars(deps: Deps, session: Session, tariff_id: TariffId) -> None:
    """Заводит заказ и показывает условия вместе со ссылкой на счёт.

    Звёздная подписка всегда регулярная: Telegram списывает сам каждый
    период, и одноразового варианта у неё нет. Поэтому и условия на экране
    заказа всегда про продление.
    """
    if deps.stars is None:
        await _payments_not_ready(deps, session)
        return

    stars = _stars_price(deps, tariff_id)
    order = await _open_order(
        deps,
        session,
        tariff_id,
        method=PaymentMethod.STARS,
        amount=stars,
        currency=STARS,
    )

    title, description = texts.invoice(tariff_id, days=deps.settings.subscription_days)
    try:
        link = await deps.stars.subscription_link(
            title=title,
            description=description,
            order_id=order.id,
            stars=stars,
            period_days=deps.settings.subscription_days,
        )
    except Exception as error:
        deps.logger.warning(
            "payment_start_failed",
            user_id=int(session.user.id),
            method=PaymentMethod.STARS.value,
            error=repr(error),
        )
        await _say(deps, session, texts.payment_failed().text)
        return

    await _show_order(
        deps,
        session,
        tariff_id,
        amount=stars,
        currency=STARS,
        url=link,
        recurring=True,
    )


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

    Продление подписки спрашивают тем же запросом, но заказ к этому моменту
    уже оплачен. Отказывать нельзя: это остановило бы подписку, за которую
    человек платит. Но и соглашаться на любой оплаченный заказ нельзя — тогда
    старая ссылка на счёт, открытая второй раз, стоила бы человеку денег без
    единого дня тарифа. Поэтому оплаченный заказ проходит только при живой
    подписке: продление бывает только у неё.
    """
    if deps.stars is None:
        return

    order = await deps.storage.get_payment(order_id) if order_id else None
    known = (
        order is not None
        and user is not None
        and order.user_id == user.id
        and (
            order.status == PaymentStatus.PENDING.value
            or (
                order.status == PaymentStatus.PAID.value
                and await _renews(deps, order.user_id)
            )
        )
    )
    if not known:
        deps.logger.warning(
            "payment_unknown_order",
            user_id=int(user.id) if user is not None else None,
        )
    await deps.stars.approve(request_id, ok=known)


async def _renews(deps: Deps, user_id: UserId) -> bool:
    """Есть ли у человека подписка, по которой ждём очередного списания."""
    subscription = await deps.storage.get_subscription(user_id)
    return (
        subscription is not None
        and subscription.status != SubscriptionStatus.CANCELLED.value
    )


async def confirm(
    deps: Deps,
    order_id: str,
    *,
    charge_id: str | None = None,
    renewal: bool = False,
) -> Payment | None:
    """Отмечает заказ оплаченным и выдаёт тариф. Возвращает заказ, если выдали.

    Сессии здесь нет намеренно: подтверждение приходит и вебхуком ЮKassa, где
    никакого «текущего пользователя» не существует. Пользователь берётся из
    самого заказа.

    ``renewal`` — очередное списание по звёздной подписке. Telegram сообщает о
    нём тем же payload'ом, что и о первом платеже, то есть ссылается на давно
    оплаченный заказ. Продлевать по нему нельзя: заказ уже закрыт, и
    ``mark_paid`` вернёт False. Поэтому на продление заводится свой заказ, а
    защитой от двойной выдачи служит идентификатор списания — он у каждого
    периода свой и уникален в базе.
    """
    order = await deps.storage.get_payment(order_id)
    if order is None:
        deps.logger.warning("payment_confirm_unknown")
        return None

    if renewal:
        order = await _renewal_order(deps, order, charge_id)
        if order is None:
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
    await _record_subscription(deps, order, charge_id=charge_id, until=expires_at)

    deps.logger.info(
        "payment_confirmed",
        user_id=int(order.user_id),
        payment_id=order.id,
        tariff=order.tariff.value,
        method=order.method,
        docs_version=order.docs_version,
    )
    # Отдельной записью и после подтверждения денег: согласие с условиями
    # человек даёт нажатием кнопки оплаты, а доказательством ему служит сам
    # платёж. Пункт 4.11 оферты требует уметь показать, кто, когда и с какой
    # редакцией согласился, — вот эта запись.
    deps.logger.info(
        "consent_accepted",
        user_id=int(order.user_id),
        payment_id=order.id,
        docs_version=order.docs_version,
        tariff=order.tariff.value,
        method=order.method,
    )
    return replace(order, paid_at=deps.now())


async def announce(
    deps: Deps, session: Session, order: Payment, *, renewal: bool = False
) -> None:
    """Говорит человеку, что тариф включён.

    Дату берём из пользователя, а не считаем заново: показать надо ровно тот
    срок, который записан, иначе экран и база разойдутся.

    Про продление говорим только тогда, когда подписка правда заведена.
    Обещание «дальше продлится само» там, где продления не будет, оставило бы
    человека без тарифа в тот день, когда он на него рассчитывал.
    """
    user = await deps.storage.get_user_by_id(order.user_id)
    until = user.tariff_expires_at if user is not None else None
    day = (
        current_day(until, deps.settings.timezone)
        if until is not None
        else deps.today()
    )
    subscription = await deps.storage.get_subscription(order.user_id)
    renewing = (
        subscription is not None
        and subscription.status != SubscriptionStatus.CANCELLED.value
    )

    if renewal:
        screen = texts.subscription_renewed(
            order.tariff,
            amount=order.amount,
            currency=order.currency,
            until=texts.format_date(day),
        )
    else:
        screen = texts.payment_done(
            order.tariff, until=texts.format_date(day), renewing=renewing
        )
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)


# --- Вспомогательное -----------------------------------------------------


async def _show_order(
    deps: Deps,
    session: Session,
    tariff_id: TariffId,
    *,
    amount: int,
    currency: str,
    url: str,
    recurring: bool,
) -> None:
    """Экран оформления заказа: условия и кнопка оплаты в одном сообщении.

    Вместе, а не по отдельности: согласие человек даёт нажатием кнопки
    оплаты (§4.11 оферты), и если условия остались в предыдущем сообщении,
    согласие получается вслепую.
    """
    next_charge = _new_expiry(
        deps,
        current=session.user.tariff_expires_at,
        bought=tariff_id,
        current_tariff=session.user.tariff,
    )
    screen = texts.payment_order(
        tariff_id,
        days=deps.settings.subscription_days,
        amount=amount,
        currency=currency,
        next_charge=texts.format_date(current_day(next_charge, deps.settings.timezone)),
        recurring=recurring,
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=Keyboard(
            rows=(
                (Button(text=texts.BUTTON_PAY_OPEN, url=url),),
                (
                    Button(text=texts.BUTTON_OFFER, url=deps.settings.offer_url),
                    Button(text=texts.BUTTON_PRIVACY, url=deps.settings.privacy_url),
                ),
            )
        ),
        show_menu=False,
    )


async def _renewal_order(
    deps: Deps, first: Payment, charge_id: str | None
) -> Payment | None:
    """Заводит заказ на очередной период звёздной подписки.

    Возвращает None, если это повтор уже обработанного списания. Опознаём его
    по идентификатору списания: он уникален в таблице заказов, поэтому
    попытка привязать его второй раз проваливается на уровне базы — то есть
    надёжно, а не «мы вроде бы проверили».
    """
    if charge_id is None:
        deps.logger.warning("renewal_without_charge_id", user_id=int(first.user_id))
        return None

    order = await deps.storage.create_payment(
        user_id=first.user_id,
        tariff=first.tariff,
        method=first.method,
        amount=first.amount,
        currency=first.currency,
        # Редакция текущая, а не та, что была при первой оплате: §4.3 оферты
        # прямо говорит, что новые условия начинают действовать со
        # следующего расчётного периода, а продление — это он и есть.
        docs_version=deps.settings.docs_version,
    )
    if not await deps.storage.attach_external_id(order.id, charge_id):
        deps.logger.info("renewal_already_confirmed", user_id=int(first.user_id))
        return None
    return order


async def _record_subscription(
    deps: Deps, order: Payment, *, charge_id: str | None, until: datetime
) -> None:
    """Заводит или продлевает подписку — если продление вообще будет.

    Решение принимается здесь, а не на экране: экран показал ровно то же
    самое, потому что оба места спрашивают об одном — умеет ли выбранный
    способ списывать сам.
    """
    current = await deps.storage.get_subscription(order.user_id)
    method_id = current.payment_method_id if current is not None else None

    if order.method == PaymentMethod.STARS.value:
        recurring = deps.stars is not None
        # Отменяет подписку Telegram по первому списанию, а не по последнему,
        # поэтому уже сохранённый идентификатор важнее нового.
        charge_id = (current.charge_id if current is not None else None) or charge_id
    elif deps.cards is not None and deps.cards.recurring:
        if method_id is None and order.external_id is not None:
            method_id = await deps.cards.saved_method_of(order.external_id)
        # Без сохранённого способа оплаты списать в следующий раз будет
        # нечем. Заводить подписку «на будущее» нельзя: она обещала бы
        # продление, которого не случится.
        recurring = method_id is not None
        if not recurring:
            deps.logger.warning(
                "subscription_without_method", user_id=int(order.user_id)
            )
    else:
        recurring = False

    if not recurring:
        return

    await deps.storage.save_subscription(
        Subscription(
            user_id=order.user_id,
            tariff=order.tariff,
            method=order.method,
            status=SubscriptionStatus.ACTIVE.value,
            amount=order.amount,
            currency=order.currency,
            next_charge_at=until,
            created_at=current.created_at if current is not None else deps.now(),
            payment_method_id=method_id,
            charge_id=charge_id,
            # Прошлые отметки относятся к прошлому списанию: о новом надо
            # предупредить заново, и цену к нему сверить заново.
            reminded_for=None,
            price_checked_for=None,
            failed_since=None,
            cancelled_at=None,
        )
    )


def _existing_charge_id(current: Subscription | None) -> str | None:
    """Идентификатор первого списания: им Telegram отменяет всю подписку."""
    return current.charge_id if current is not None else None


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


async def _open_order(
    deps: Deps,
    session: Session,
    tariff_id: TariffId,
    *,
    method: PaymentMethod,
    amount: int,
    currency: str,
) -> Payment:
    """Заводит заказ вместе с редакцией документов, показанных человеку.

    Версия хранится в самом заказе и живёт пять лет — столько же, сколько
    данные о платежах. Именно она, а не текущая настройка, попадёт потом в
    запись о согласии: спорить придётся о том, что человек видел, а не о том,
    что опубликовано сегодня.
    """
    return await deps.storage.create_payment(
        user_id=session.user.id,
        tariff=tariff_id,
        method=method.value,
        amount=amount,
        currency=currency,
        docs_version=deps.settings.docs_version,
    )


def _stars_price(deps: Deps, tariff_id: TariffId) -> int:
    return stars_price(
        tariff_of(tariff_id),
        markup=deps.settings.stars_markup,
        rub_per_star=deps.settings.rub_per_star,
    )


def _method_keyboard(tariff_id: TariffId) -> Keyboard:
    """Два способа оплаты в один ряд. Условия — на следующем экране."""
    return Keyboard.row(
        Button(
            text=texts.BUTTON_PAY_CARD,
            action=method_action(PaymentMethod.CARD.value, tariff_id.value),
        ),
        Button(
            text=texts.BUTTON_PAY_STARS,
            action=method_action(PaymentMethod.STARS.value, tariff_id.value),
        ),
    )


async def _payments_not_ready(deps: Deps, session: Session) -> None:
    """Оплата не настроена. Тупика быть не должно и здесь."""
    screen = texts.payments_soon()
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.payments_soon()
    )


async def _say(deps: Deps, session: Session, text: str) -> None:
    await deps.messenger.send_text(session.chat, text, show_menu=True)
