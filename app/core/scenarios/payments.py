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

from app.core import pending, texts
from app.core.actions import TRIAL_TARGET, email_action, method_action
from app.core.emails import normalise_email
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
from app.core.receipts import Receipt, receipt_for
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import RUB, STARS, TRIAL, stars_price, tariff_of
from app.ports.payments import PaymentMethod, PaymentStatus, SubscriptionStatus
from app.ports.storage import GrantOutcome


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

    if await _renews_otherwise(deps, session, PaymentMethod.CARD):
        return

    if deps.settings.receipts_ready and session.user.email is None:
        # Чек по 54-ФЗ доставляется на почту, и другого способа его вручить у
        # нас нет. Спрашиваем до заказа, а не после оплаты: человек, уже
        # отдавший деньги, вправе не отвечать на наши вопросы — а чек ему всё
        # равно должен уйти.
        await ask_for_email(deps, session, tariff_id)
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
    # Сохранять способ оплаты просим только тогда, когда собираемся им
    # пользоваться. Иначе провайдер хранил бы карту человека без причины, а
    # мы обещали бы продление, которого не будет.
    url = await _card_link(deps, session, order, save_method=recurring)
    if url is None:
        return

    await _show_order(
        deps,
        session,
        tariff_id,
        amount=tariff.price_rub,
        currency=RUB,
        url=url,
        recurring=recurring,
    )


async def trial_offered(deps: Deps, session: Session) -> bool:
    """Положен ли человеку пробный период «Лайта» прямо сейчас (ПП1, ПП7).

    Только при включённой настройке, опубликованных документах и магазине с
    автоплатежами: без них 299 ₽ после пробных дней списать было бы нечем,
    а на звёздах пробного периода нет вовсе. И только тому, кто ещё ни разу
    не платил и пробного периода не брал. Человек перечитывается: снимок в
    сессии мог устареть на оплате, которая прошла минуту назад.

    «Уже брал пробный период» отдельно не проверяется: заказ на 1 ₽ — тоже
    оплата, и после него человек в числе плативших.
    """
    if not deps.settings.trial_enabled or not deps.settings.documents_ready:
        return False
    if deps.cards is None or not deps.cards.recurring:
        return False
    return not await deps.storage.ever_paid(session.user.id)


async def start_trial(deps: Deps, session: Session) -> None:
    """Заводит заказ на пробный период и показывает условия с кнопкой оплаты.

    Кнопка предложения живёт в переписке, и нажать её можно и после оплаты,
    и после того, как настройку выключили. Тогда — обычные тарифы, а не счёт
    на пробный период, которого не будет.
    """
    if deps.cards is None or not await trial_offered(deps, session):
        await deps.messenger.send_text(
            session.chat,
            texts.tariffs_screen(with_presentations=deps.presentations_on).text,
            keyboard=keyboards.tariffs(),
            show_menu=False,
        )
        return

    if deps.settings.receipts_ready and session.user.email is None:
        # Чек нужен и на 1 ₽ (ПП8) — почту спрашиваем до заказа.
        await ask_for_email(deps, session, TRIAL.tariff, trial=True)
        return

    order = await _open_order(
        deps,
        session,
        TRIAL.tariff,
        method=PaymentMethod.CARD,
        amount=TRIAL.price_rub,
        currency=RUB,
        trial=True,
    )
    # Карта сохраняется всегда: ради неё пробный период и затевается — через
    # три дня по ней списывается полная цена.
    url = await _card_link(deps, session, order, save_method=True)
    if url is None:
        return

    receipt_to = session.user.email or "" if deps.settings.receipts_ready else ""
    first_charge = deps.now() + timedelta(days=TRIAL.days)
    screen = texts.trial_order(
        first_charge=texts.format_date(
            current_day(first_charge, deps.settings.timezone)
        ),
        statement=deps.settings.bank_statement_name,
        receipt_to=receipt_to,
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=_order_keyboard(
            deps, email_target=TRIAL_TARGET, url=url, receipt_to=receipt_to
        ),
        show_menu=False,
    )


async def _card_link(
    deps: Deps, session: Session, order: Payment, *, save_method: bool
) -> str | None:
    """Создаёт платёж картой по заказу и возвращает ссылку на оплату.

    None — ссылки нет, и человеку уже сказано, что оплата не открылась.
    Сумма и тариф берутся из заказа: чек и платёж обязаны совпасть с тем,
    что записано, — у ЮKassa расхождение суммы чека и платежа — ошибка.
    """
    assert deps.cards is not None
    try:
        intent = await deps.cards.create_payment(
            order_id=order.id,
            amount_rub=order.amount,
            description=texts.invoice(
                order.tariff, days=deps.settings.subscription_days
            )[0],
            save_method=save_method,
            receipt=receipt_for_order(
                deps,
                email=session.user.email,
                tariff_id=order.tariff,
                amount_rub=order.amount,
            ),
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
        return None

    if not await deps.storage.attach_external_id(order.id, intent.external_id):
        # Платёж уже привязан к другому заказу. Ссылку отдавать нельзя:
        # подтвердить по ней оплату мы не сможем, а деньги человек отдаст.
        deps.logger.error("payment_id_taken", user_id=int(session.user.id))
        await _say(deps, session, texts.payment_failed().text)
        return None

    if intent.confirmation_url is None:
        deps.logger.error("payment_without_url", user_id=int(session.user.id))
        await _say(deps, session, texts.payment_failed().text)
        return None
    return intent.confirmation_url


async def ask_for_email(
    deps: Deps, session: Session, tariff_id: TariffId, *, trial: bool = False
) -> None:
    """Просит почту и запоминает, к оплате какого тарифа потом вернуться.

    Одна точка на оба повода: адреса ещё нет вовсе или человек нажал «Другая
    почта», заметив опечатку на экране заказа. ``trial`` — вернуться надо к
    пробному периоду, а не к обычной оплате тарифа.
    """
    target = TRIAL_TARGET if trial else tariff_id.value
    await deps.storage.set_pending(session.user.id, pending.await_email(target))
    await deps.messenger.send_text(session.chat, texts.email_ask().text)


async def remember_email(deps: Deps, session: Session, written: str) -> None:
    """Принимает почту для чека и возвращает человека к оплате.

    Ожидание снимается только на годном адресе. Иначе опечатка выкидывала бы
    человека из покупки в обычный чат, и он бы даже не понял, что произошло.
    """
    target = pending.parse_await_email(session.user.pending)
    tariff_id = TRIAL.tariff if target == TRIAL_TARGET else _tariff(target)
    if tariff_id is None:
        # Ожидание от версии, где тариф назывался иначе. Возвращаем к выбору.
        await _clear_pending(deps, session)
        await deps.messenger.send_text(
            session.chat,
            texts.tariffs_screen(with_presentations=deps.presentations_on).text,
            keyboard=keyboards.tariffs(),
        )
        return

    email = normalise_email(written)
    if email is None:
        await deps.messenger.send_text(session.chat, texts.email_bad().text)
        return

    await deps.storage.set_email(session.user.id, email)
    await deps.storage.set_pending(session.user.id, None)
    # Адрес логировать нельзя, а знать, что человек его дал, полезно.
    deps.logger.info("receipt_email_saved", user_id=int(session.user.id))

    with_email = replace(session, user=replace(session.user, email=email))
    if target == TRIAL_TARGET:
        await start_trial(deps, with_email)
        return
    await start_card(deps, with_email, tariff_id)


def _tariff(tariff_id: str | None) -> TariffId | None:
    if tariff_id is None:
        return None
    try:
        return TariffId(tariff_id)
    except ValueError:
        return None


async def _clear_pending(deps: Deps, session: Session) -> None:
    if session.user.pending is not None:
        await deps.storage.set_pending(session.user.id, None)


def receipt_for_order(
    deps: Deps,
    *,
    email: str | None,
    tariff_id: TariffId,
    amount_rub: int,
) -> Receipt | None:
    """Чек к заказу. None — чеки через ЮKassa не формируются.

    Одна точка сборки на первый платёж и на автосписание: закон не делает
    скидки на то, что при продлении человека нет за экраном, и разъехаться
    этим двум чекам нельзя.
    """
    if deps.settings.fiscal is None:
        return None
    if email is None:
        # Сюда попасть не должны: почта спрашивается до заказа. Но платить
        # без чека нельзя, а тихо отправить чек в никуда — тем более.
        raise ValueError("нет почты покупателя для чека")
    return receipt_for(
        email=email,
        description=texts.invoice(tariff_id, days=deps.settings.subscription_days)[0],
        amount_rub=amount_rub,
        currency=RUB,
        fiscal=deps.settings.fiscal,
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

    if await _renews_otherwise(deps, session, PaymentMethod.STARS):
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


#: Сколько раз пересчитать срок, если его поменяли во время выдачи. Это
#: два заказа одного человека в одну и ту же секунду — третьего не бывает.
_GRANT_ATTEMPTS = 3


async def _renews_otherwise(
    deps: Deps, session: Session, method: PaymentMethod
) -> bool:
    """Продлевается ли подписка другим способом — и если да, сказать об этом.

    Подписка у человека одна, но списания у двух способов независимы:
    звёздную продлевает сам Telegram, карточную — мы. Оплата вторым способом
    заменила бы нашу запись о подписке, но не остановила бы первую, и человек
    платил бы дважды за один срок. Сменить способ можно — через отключение
    продления: тогда второго списания не будет.

    Проверка стоит в начале оплаты каждым способом, а не на экране выбора:
    кнопки способов живут в переписке, и нажать старую можно когда угодно.
    """
    subscription = await deps.storage.get_subscription(session.user.id)
    if (
        subscription is None
        or subscription.status == SubscriptionStatus.CANCELLED.value
        or subscription.method == method.value
    ):
        return False

    deps.logger.info(
        "payment_other_method_renews",
        user_id=int(session.user.id),
        method=subscription.method,
    )
    screen = texts.subscription_other_method(
        by_stars=subscription.method == PaymentMethod.STARS.value
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.subscription_manage()
    )
    return True


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

    if order.status != PaymentStatus.PENDING.value:
        # Уведомления об оплате приходят по несколько раз. Это не ошибка —
        # просто продлевать подписку на каждое нельзя.
        deps.logger.info("payment_already_confirmed", user_id=int(order.user_id))
        return None

    # Всё, что спрашивается у провайдера, — до выдачи. Выдача — одна
    # транзакция хранилища (П4), и ждать в ней чужую сеть нельзя.
    previous = await deps.storage.get_subscription(order.user_id)
    subscription = await _subscription_for(
        deps, order, charge_id=charge_id, renewal=renewal
    )
    if order.trial and subscription is not None:
        # После пробных дней списывается полная цена тарифа, и перед этим
        # первым списанием человека обязательно предупреждают (ПП4).
        subscription = replace(
            subscription,
            amount=tariff_of(order.tariff).price_rub,
            remind_before_charge=True,
        )

    for _ in range(_GRANT_ATTEMPTS):
        user = await deps.storage.get_user_by_id(order.user_id)
        if user is None:
            deps.logger.error("payment_user_missing", user_id=int(order.user_id))
            return None
        expires_at = (
            deps.now() + timedelta(days=TRIAL.days)
            if order.trial
            else _new_expiry(
                deps,
                current=user.tariff_expires_at,
                bought=order.tariff,
                current_tariff=user.tariff,
            )
        )
        outcome = await deps.storage.complete_payment(
            order.id,
            tariff=order.tariff,
            expires_at=expires_at,
            seen_tariff=user.tariff,
            seen_expiry=user.tariff_expires_at,
            subscription=(
                replace(subscription, next_charge_at=expires_at)
                if subscription is not None
                else None
            ),
            # Каждая оплата и каждое продление начинают период месячной нормы
            # заново: человек заплатил — норма полная.
            norm_since=deps.now(),
            trial=order.trial,
        )
        if outcome is GrantOutcome.GRANTED:
            break
        if outcome is GrantOutcome.TRIAL_USED:
            # Человек оплатил вторую ссылку на пробный период, или уже платил.
            # Деньги взяты, заказ оплачен, но второго пробного периода нет:
            # 1 ₽ возвращается в кабинете ЮKassa, по этому номеру заказа.
            deps.logger.error(
                "trial_payment_unused", user_id=int(order.user_id), payment_id=order.id
            )
            return None
        if outcome is GrantOutcome.ALREADY:
            deps.logger.info("payment_already_confirmed", user_id=int(order.user_id))
            return None
        # STALE: срок поменялся, пока считали, — пересчитываем от нового.
    else:
        # Заказ остался pending — его доведёт следующее уведомление или сверка.
        deps.logger.error("payment_grant_contended", user_id=int(order.user_id))
        return None

    if not renewal:
        await _end_previous(deps, user, previous, replaced_by=subscription)

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


async def refunded(deps: Deps, order: Payment) -> None:
    """Деньги по заказу вернули: оплаченный ими месяц кончается (Т5).

    Продление снимается сразу: списать с того, кому только что вернули
    деньги, — верный способ получить оспаривание платежа вместо покупателя.

    Срок тарифа укорачивается на тот месяц, за который вернули деньги, — и
    только если этот месяц ещё идёт и тариф тот же. Вернули за давно
    прошедший месяц — отбирать нечего: текущий оплачен другим заказом.
    Вернули за оплаченный заранее следующий месяц — текущий остаётся. Вернули
    за единственный — тариф кончается сейчас, и вместе с ним платная норма:
    дальше действуют бесплатные.

    Заказ на пробный период — то же самое, только срок у него пробный. А
    вернули лишний 1 ₽, по которому пробного периода не выдавали (вторая
    оплаченная ссылка), — отбирать и отменять нечего.
    """
    user = await deps.storage.get_user_by_id(order.user_id)
    if order.trial and (user is None or user.trial_order_id != order.id):
        deps.logger.info("trial_refund_unused", user_id=int(order.user_id))
        return

    await deps.storage.cancel_subscription(order.user_id, deps.now())

    days = TRIAL.days if order.trial else deps.settings.subscription_days
    term = timedelta(days=days)
    now = deps.now()
    if (
        user is None
        or order.paid_at is None
        or order.paid_at + term <= now
        or user.tariff is not order.tariff
        or user.tariff_expires_at is None
        or user.tariff_expires_at <= now
    ):
        return

    shortened = user.tariff_expires_at - term
    if shortened <= now:
        await deps.storage.set_tariff(user.id, TariffId.FREE, None)
    else:
        await deps.storage.set_tariff(user.id, user.tariff, shortened)
    deps.logger.info("payment_refund_revoked", user_id=int(user.id))


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

    if order.trial:
        screen = texts.trial_started(
            until=texts.format_date(day), amount=tariff_of(order.tariff).price_rub
        )
    elif renewal:
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
    # Адрес человек назвал парой сообщений раньше и мог ошибиться в букве.
    # Увидев его перед оплатой, ошибку он заметит; получив пустоту вместо
    # чека — уже нет. У звёзд чека от нас не бывает вовсе.
    receipt_to = (
        session.user.email or ""
        if currency == RUB and deps.settings.receipts_ready
        else ""
    )
    screen = texts.payment_order(
        tariff_id,
        days=deps.settings.subscription_days,
        amount=amount,
        currency=currency,
        next_charge=texts.format_date(current_day(next_charge, deps.settings.timezone)),
        recurring=recurring,
        # Только для рублёвой оплаты: у звёзд списывает мессенджер, и никакой
        # банковской выписки, в которой человек мог бы не узнать платёж, не
        # существует.
        statement=deps.settings.bank_statement_name if currency == RUB else "",
        receipt_to=receipt_to,
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=_order_keyboard(
            deps, email_target=tariff_id.value, url=url, receipt_to=receipt_to
        ),
        show_menu=False,
    )


def _order_keyboard(
    deps: Deps, *, email_target: str, url: str, receipt_to: str
) -> Keyboard:
    """Кнопки экрана заказа: оплатить, прочитать условия, поправить почту."""
    rows: list[tuple[Button, ...]] = [
        (Button(text=texts.BUTTON_PAY_OPEN, url=url),),
        (
            Button(text=texts.BUTTON_OFFER, url=deps.settings.offer_url),
            Button(text=texts.BUTTON_PRIVACY, url=deps.settings.privacy_url),
        ),
    ]
    if receipt_to:
        # Только там, где адрес показан. Предлагать «другую почту» тому, у
        # кого её и не спрашивали, — обещать шаг, которого нет.
        rows.append(
            (
                Button(
                    text=texts.BUTTON_EMAIL_CHANGE,
                    action=email_action(email_target),
                ),
            )
        )
    return Keyboard(rows=tuple(rows))


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


async def _end_previous(
    deps: Deps,
    user: User,
    previous: Subscription | None,
    *,
    replaced_by: Subscription | None,
) -> None:
    """Прекращает прежнюю подписку, если новая оплата — не она (Т0).

    Правило одно: человек никогда не платит по двум подпискам сразу. На входе
    в оплату его не удержать целиком — ссылку на карту можно взять до звёзд, а
    оплатить после, и звёздами на другой тариф Telegram заводит вторую
    подписку, а не меняет первую. Поэтому решает подтверждение: деньги за
    новую уже взяты, а прежняя отменяется у того, кто по ней списывает.

    Зовётся после выдачи, а не до неё: новая оплата подтверждена, и тариф
    человек получает в любом случае — даже если Telegram отменить не дал.
    Такой сбой — не повод не выдать: прежняя подписка встаёт в очередь, и
    отмену повторяет каждый проход биллинга, пока она не пройдёт
    (``subscriptions.cancel_replaced``).
    """
    if previous is None or previous.status == SubscriptionStatus.CANCELLED.value:
        return
    if _same_subscription(previous, replaced_by):
        return

    if previous.method == PaymentMethod.STARS.value:
        # Карточную отменять у провайдера нечего: списываем по ней мы сами, и
        # новая запись о подписке (или отметка об отмене ниже) её остановит.
        # Звёздную списывает Telegram, и наша запись его ни к чему не обязывает.
        if previous.charge_id is None:
            # Без идентификатора первого списания Telegram отменить не даст,
            # и повтор тут не поможет: такого быть не должно вовсе.
            deps.logger.error("subscription_replace_impossible", user_id=int(user.id))
        else:
            try:
                if deps.stars is None:
                    raise RuntimeError("звёзды сейчас выключены")
                await deps.stars.cancel(
                    user_id=user.external_id, charge_id=previous.charge_id
                )
            except Exception as error:
                deps.logger.warning(
                    "subscription_replace_failed",
                    user_id=int(user.id),
                    error=repr(error),
                )
                await deps.storage.queue_star_cancel(
                    user.id, previous.charge_id, deps.now()
                )
            else:
                deps.logger.info("subscription_replaced", user_id=int(user.id))

    if replaced_by is None:
        # Новая оплата разовая: заменить прежнюю записью нечем, а оставить её
        # действующей значит списать по ней в следующем месяце.
        await deps.storage.cancel_subscription(user.id, deps.now())


def _same_subscription(previous: Subscription, new: Subscription | None) -> bool:
    """Продолжает ли новая запись ту же подписку, а не заводит вторую.

    Карточная подписка у человека одна по построению: списываем мы, и смена
    тарифа картой — та же запись с другим тарифом. Звёздная — по списанию,
    которым её завёл Telegram: другой идентификатор — другая подписка.
    """
    if new is None or new.method != previous.method:
        return False
    if previous.method == PaymentMethod.STARS.value:
        return new.charge_id == previous.charge_id
    return True


async def _subscription_for(
    deps: Deps, order: Payment, *, charge_id: str | None, renewal: bool = False
) -> Subscription | None:
    """Подписка, которую заведёт или перенесёт выдача; None — продления нет.

    Решение принимается здесь, а не на экране: экран показал ровно то же
    самое, потому что оба места спрашивают об одном — умеет ли выбранный
    способ списывать сам.
    """
    current = await deps.storage.get_subscription(order.user_id)
    method_id = (
        current.payment_method_id
        if current is not None and current.method == PaymentMethod.CARD.value
        else None
    )

    if order.method == PaymentMethod.STARS.value:
        recurring = deps.stars is not None
        method_id = None
        if renewal:
            # Отменяет подписку Telegram по первому списанию, а не по
            # последнему, поэтому у продления важнее уже сохранённый. Новая
            # оплата — это новая подписка у Telegram, и отменять её придётся
            # по её собственному списанию.
            charge_id = (
                current.charge_id if current is not None else None
            ) or charge_id
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
        return None

    # Срок списания ставит вызывающий: он считается внутри выдачи, от того
    # срока тарифа, который выдача и запишет.
    return Subscription(
        user_id=order.user_id,
        tariff=order.tariff,
        method=order.method,
        status=SubscriptionStatus.ACTIVE.value,
        amount=order.amount,
        currency=order.currency,
        next_charge_at=deps.now(),
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
    trial: bool = False,
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
        trial=trial,
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
