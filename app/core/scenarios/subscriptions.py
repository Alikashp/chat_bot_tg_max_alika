"""Жизнь подписки после первой оплаты (§4.9–4.17 оферты).

Первая оплата — это оферта, всё остальное — её исполнение, и правил здесь
ровно столько, сколько мы на себя взяли:

* предупредить о списании не позднее чем за сутки (§4.13);
* дать отключить продление в любой момент, из профиля (§4.14);
* при отключении не отбирать оплаченное — оно дорабатывает (§4.15);
* при неудачном списании пробовать три дня и сказать об этом (§4.16);
* о новой цене предупредить за неделю (§4.17).

Каждое из них написано против одного и того же соблазна — промолчать. Тихо
списать, тихо перестать списывать, тихо поднять цену. Все функции ниже
существуют затем, чтобы человек узнавал о деньгах раньше, чем банк.

Сессии в фоновых сценариях нет: разговор начинаем мы, а не человек. Она
собирается из пользователя (``session_for``), потому что бот личный и чат
совпадает с самим человеком.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from app.core import texts
from app.core.limits import current_day
from app.core.models import Payment, StarCancel, Subscription, User
from app.core.scenarios import keyboards, payments
from app.core.scenarios.deps import Deps, Session, session_for
from app.core.tariffs import RUB, tariff_of
from app.ports.payments import (
    CardPayments,
    ChargeStatus,
    PaymentMethod,
    PaymentStatus,
    SubscriptionStatus,
)


async def show(deps: Deps, session: Session) -> None:
    """Экран подписки: сколько, когда и как отключить (§4.14 оферты)."""
    subscription = await deps.storage.get_subscription(session.user.id)
    if subscription is None:
        screen = texts.subscription_none()
        await deps.messenger.send_text(
            session.chat, screen.text, keyboard=keyboards.tariffs_or_profile()
        )
        return

    until = _until(deps, session.user, subscription)

    if subscription.status == SubscriptionStatus.CANCELLED.value:
        screen = texts.subscription_stopped(subscription.tariff, until=until)
        keyboard = keyboards.tariffs_or_profile()
    elif subscription.status == SubscriptionStatus.PAST_DUE.value:
        screen = texts.subscription_failing(
            subscription.tariff,
            amount=subscription.amount,
            currency=subscription.currency,
        )
        keyboard = keyboards.subscription_manage()
    else:
        screen = texts.subscription_active(
            subscription.tariff,
            days=deps.settings.subscription_days,
            amount=subscription.amount,
            currency=subscription.currency,
            next_charge=texts.format_date(
                current_day(subscription.next_charge_at, deps.settings.timezone)
            ),
        )
        keyboard = keyboards.subscription_manage()

    await deps.messenger.send_text(session.chat, screen.text, keyboard=keyboard)


async def cancel(deps: Deps, session: Session) -> None:
    """Отключает автопродление (§4.14 оферты).

    Порядок здесь важнее любого текста. Сначала отменяем у того, кто на самом
    деле списывает деньги, и только потом у себя. Наоборот — значит показать
    человеку «больше не спишем» и всё равно списать: наша отметка Telegram ни
    к чему не обязывает.
    """
    subscription = await deps.storage.get_subscription(session.user.id)
    if (
        subscription is None
        or subscription.status == SubscriptionStatus.CANCELLED.value
    ):
        # Отменять нечего: подписки нет или её уже отменили. Показываем
        # текущее положение дел вместо отчёта об ошибке.
        await show(deps, session)
        return

    if subscription.method == PaymentMethod.STARS.value:
        if deps.stars is None or subscription.charge_id is None:
            deps.logger.error(
                "subscription_cancel_impossible", user_id=int(session.user.id)
            )
            await _say(deps, session, texts.subscription_cancel_failed())
            return
        try:
            await deps.stars.cancel(
                user_id=session.user.external_id, charge_id=subscription.charge_id
            )
        except Exception as error:
            deps.logger.error(
                "subscription_cancel_failed",
                user_id=int(session.user.id),
                error=repr(error),
            )
            await _say(deps, session, texts.subscription_cancel_failed())
            return

    if not await deps.storage.cancel_subscription(session.user.id, deps.now()):
        await show(deps, session)
        return

    deps.logger.info(
        "subscription_cancelled",
        user_id=int(session.user.id),
        tariff=subscription.tariff.value,
        method=subscription.method,
    )
    screen = texts.subscription_cancelled(
        subscription.tariff, until=_until(deps, session.user, subscription)
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.tariffs_or_profile()
    )


# --- Фоновые сценарии ----------------------------------------------------


async def remind(deps: Deps, subscription: Subscription) -> None:
    """Предупреждает о списании за сутки (§4.13 оферты).

    Отметку ставим после отправки, а не до. Порядок выбран в пользу лишнего
    напоминания: пропустить обязательное предупреждение хуже, чем прислать
    его дважды.
    """
    user = await deps.storage.get_user_by_id(subscription.user_id)
    if user is None:
        deps.logger.error(
            "subscription_user_missing", user_id=int(subscription.user_id)
        )
        return

    screen = texts.subscription_reminder(
        subscription.tariff,
        amount=subscription.amount,
        currency=subscription.currency,
        on=texts.format_date(
            current_day(subscription.next_charge_at, deps.settings.timezone)
        ),
    )
    await deps.messenger.send_text(
        session_for(deps, user).chat,
        screen.text,
        keyboard=keyboards.subscription_manage(),
    )
    await deps.storage.mark_reminded(subscription.user_id, subscription.next_charge_at)
    deps.logger.info("subscription_reminded", user_id=int(subscription.user_id))


async def cancel_replaced(deps: Deps, pending: StarCancel) -> None:
    """Повторяет отмену прежней звёздной подписки, которую Telegram не принял.

    Без неё человек платит звёздами за две подписки сразу: новая оплата уже
    заменила прежнюю у нас, но списывает Telegram, а не мы. Повтор — каждый
    проход биллинга, пока отмена не пройдёт: ручной отмены у заказчика нет.
    С очереди подписка снимается только после принятой отмены.
    """
    user = await deps.storage.get_user_by_id(pending.user_id)
    if user is None:
        await deps.storage.star_cancel_done(pending.charge_id)
        return
    if deps.stars is None:
        deps.logger.warning("subscription_replace_waiting", user_id=int(user.id))
        return
    try:
        await deps.stars.cancel(user_id=user.external_id, charge_id=pending.charge_id)
    except Exception as error:
        deps.logger.warning(
            "subscription_replace_failed", user_id=int(user.id), error=repr(error)
        )
        return
    await deps.storage.star_cancel_done(pending.charge_id)
    deps.logger.info("subscription_replaced", user_id=int(user.id))


async def check_price(deps: Deps, subscription: Subscription) -> None:
    """Сверяет цену тарифа с той, на которую человек соглашался (§4.17 оферты).

    Если цена изменилась, предупреждаем и записываем новую в подписку: со
    следующего периода списывать будем по ней. Отметку о сверке ставим в
    любом случае — иначе один и тот же вопрос решался бы каждый тик
    планировщика всю неделю до списания.
    """
    price = tariff_of(subscription.tariff).price_rub
    changed = subscription.currency == RUB and price != subscription.amount

    if changed:
        user = await deps.storage.get_user_by_id(subscription.user_id)
        if user is None:
            deps.logger.error(
                "subscription_user_missing", user_id=int(subscription.user_id)
            )
            return
        screen = texts.subscription_price_changed(
            subscription.tariff,
            was=subscription.amount,
            now=price,
            currency=subscription.currency,
            on=texts.format_date(
                current_day(subscription.next_charge_at, deps.settings.timezone)
            ),
        )
        await deps.messenger.send_text(
            session_for(deps, user).chat,
            screen.text,
            keyboard=keyboards.subscription_manage(),
        )
        await deps.storage.advance_subscription(
            subscription.user_id,
            next_charge_at=subscription.next_charge_at,
            status=subscription.status,
            failed_since=subscription.failed_since,
            amount=price,
        )
        deps.logger.info(
            "subscription_price_changed", user_id=int(subscription.user_id)
        )

    await deps.storage.mark_price_checked(
        subscription.user_id, subscription.next_charge_at
    )


async def charge(deps: Deps, subscription: Subscription) -> None:
    """Списывает за очередной период — или разбирается, почему не вышло."""
    user = await deps.storage.get_user_by_id(subscription.user_id)
    if user is None:
        deps.logger.error(
            "subscription_user_missing", user_id=int(subscription.user_id)
        )
        return

    # Копия подписки прочитана в начале прохода, а до денег отсюда ещё
    # несколько обращений наружу. За это время человек успевает отменить
    # продление — и списать после этого значило бы взять деньги у того, кто
    # от них отказался. Перечитываем перед самым платежом.
    current = await deps.storage.get_subscription(subscription.user_id)
    if current is None or current.status == SubscriptionStatus.CANCELLED.value:
        deps.logger.info("subscription_cancelled_meanwhile", user_id=int(user.id))
        return
    subscription = current

    if subscription.method == PaymentMethod.STARS.value:
        await _await_stars(deps, subscription, user)
        return

    method_id = subscription.payment_method_id
    if method_id is None:
        # Списывать нечем у самого человека: способа оплаты не сохранено.
        # Тянуть подписку, по которой не будет денег, нельзя — она обещает
        # тариф, которого человек не получит.
        deps.logger.error(
            "subscription_cannot_charge", user_id=int(subscription.user_id)
        )
        await _end(deps, subscription, user)
        return

    cards = deps.cards
    if cards is None or not cards.recurring:
        # Магазин не настроен: пропали ключи или выключены автоплатежи. Это
        # наша поломка, а не решение человека, и прекращать из-за неё
        # подписку нельзя: отмена стирает сохранённую карту, и после починки
        # продлить было бы нечем. Пропускаем проход и кричим в лог — срок
        # остался прежним, и следующий проход попробует снова.
        deps.logger.error(
            "subscription_shop_not_configured", user_id=int(subscription.user_id)
        )
        return

    if subscription.charge_order_id is not None:
        # Списание за этот период уже начато, и чем оно кончилось, неизвестно.
        # О нём человека уже предупредили — переносить и напоминать заново
        # незачем; надо узнать исход.
        await _settle(
            deps, cards, subscription, user, subscription.charge_order_id, method_id
        )
        return

    if subscription.reminded_for != subscription.next_charge_at:
        # О списании обязаны предупредить не позднее чем за сутки (§4.13
        # оферты). Сюда мы попадаем, только если проход напоминаний это окно
        # проспал — например, сервис лежал сутки. Списать сейчас значило бы
        # взять деньги молча; поэтому переносим срок на сутки вперёд и
        # предупреждаем. Сутки бесплатной работы дешевле нарушенного обещания.
        deferred = replace(
            subscription,
            next_charge_at=deps.now() + timedelta(hours=deps.settings.reminder_hours),
        )
        if not await deps.storage.advance_subscription(
            deferred.user_id,
            next_charge_at=deferred.next_charge_at,
            status=deferred.status,
            failed_since=deferred.failed_since,
        ):
            return
        deps.logger.warning("subscription_charge_deferred", user_id=int(user.id))
        await remind(deps, deferred)
        return

    if deps.settings.receipts_ready and user.email is None:
        # Чек обязателен и при автосписании, а доставить его некуда. Списать
        # без чека нельзя: штраф за недоставленный дороже месяца подписки.
        # Такого быть не должно — почта спрашивается до первой оплаты, — но
        # если случилось, деньги не трогаем и оставляем след в логе.
        deps.logger.error("subscription_charge_without_receipt", user_id=int(user.id))
        return

    order = await deps.storage.create_payment(
        user_id=subscription.user_id,
        tariff=subscription.tariff,
        method=subscription.method,
        amount=subscription.amount,
        currency=subscription.currency,
        docs_version=deps.settings.docs_version,
    )
    # Период занимается заказом до первого обращения к провайдеру: после него
    # исход может оказаться неизвестным, и повторять придётся ровно этот
    # заказ. Упади мы между созданием заказа и этой отметкой — заказ
    # останется брошенным, но к провайдеру с ним никто не ходил, и денег по
    # нему не было.
    if not await deps.storage.hold_charge_order(subscription.user_id, order.id):
        deps.logger.warning("subscription_charge_order_busy", user_id=int(user.id))
        return
    await _attempt(deps, cards, subscription, user, order, method_id)


# --- Один период — один заказ (П8) ---------------------------------------
#
# Пока исход списания неизвестен — провайдер не ответил, ответ потерялся,
# процесс перезапустился, платёж ещё в обработке, — новый заказ не заводится.
# Новый заказ означал бы новый ключ идемпотентности, а провайдер, которому
# первый запрос дошёл, честно списал бы второй раз. Повтор идёт с тем же
# заказом, пока провайдер помнит ключ; новый заказ — только после явного
# отказа банка.

#: Запас до конца окна идемпотентности. Запрос, отправленный за минуту до
#: истечения ключа, может дойти до провайдера уже после — и стать новым
#: платежом.
_KEY_MARGIN = timedelta(hours=1)


async def _settle(
    deps: Deps,
    cards: CardPayments,
    subscription: Subscription,
    user: User,
    order_id: str,
    method_id: str,
) -> None:
    """Доводит заказ периода, исход которого остался неизвестным."""
    order = await deps.storage.get_payment(order_id)
    if order is None or order.status != PaymentStatus.PENDING.value:
        # Заказ уже закрыт — выдача освобождает период сама, так что сюда
        # попадает только опоздавшая отметка. Держать её дальше значило бы
        # больше никогда не списать.
        deps.logger.warning("subscription_charge_order_stale", user_id=int(user.id))
        await deps.storage.release_charge_order(subscription.user_id, order_id)
        return

    if order.external_id is not None:
        # Платёж у провайдера уже есть. Его не создают заново, а спрашивают,
        # чем он кончился.
        try:
            status = await cards.charge_status(order.external_id)
        except Exception as error:
            deps.logger.warning(
                "subscription_charge_error",
                user_id=int(user.id),
                error=repr(error),
            )
            await _retry_same(deps, subscription)
            return
        await _resolve(deps, subscription, user, order, status)
        return

    if deps.now() >= order.created_at + cards.idempotence_window - _KEY_MARGIN:
        # Провайдер так и не ответил, а ключ он вот-вот забудет. Повтор с
        # забытым ключом — это новый платёж, новый заказ — тоже. Денег больше
        # не трогаем: продление прекращаем, а заказ оставляем открытым — если
        # первое списание всё-таки прошло, его закроет уведомление ЮKassa.
        deps.logger.error(
            "subscription_charge_unresolved",
            user_id=int(user.id),
            payment_id=order.id,
        )
        await _end(deps, subscription, user)
        return

    await _attempt(deps, cards, subscription, user, order, method_id)


async def _attempt(
    deps: Deps,
    cards: CardPayments,
    subscription: Subscription,
    user: User,
    order: Payment,
    method_id: str,
) -> None:
    """Просит провайдера списать по заказу периода.

    Всё, что уходит провайдеру, берётся из заказа, а не из подписки: повтор
    с тем же ключом, но другой суммой ЮKassa отвергнет — и исход снова
    останется неизвестным.
    """
    try:
        result = await cards.charge_saved(
            order_id=order.id,
            amount_rub=order.amount,
            description=texts.invoice(
                order.tariff, days=deps.settings.subscription_days
            )[0],
            payment_method_id=method_id,
            receipt=payments.receipt_for_order(
                deps,
                email=user.email,
                tariff_id=order.tariff,
                amount_rub=order.amount,
            ),
        )
    except Exception as error:
        # Провайдер не ответил. Это не отказ банка: неизвестно, списали ли
        # деньги, и считать попытку неудачной нельзя — иначе сбой сети у нас
        # стоил бы человеку подписки. Повторяем тот же заказ, и скоро.
        deps.logger.warning(
            "subscription_charge_error",
            user_id=int(subscription.user_id),
            error=repr(error),
        )
        await _retry_same(deps, subscription)
        return

    if result.external_id is not None and not await deps.storage.attach_external_id(
        order.id, result.external_id
    ):
        # Платёж провайдера уже привязан к другому заказу. Ключом служит наш
        # заказ, так что такого быть не должно; выдавать по нему период
        # нельзя, а разбираться — руками.
        deps.logger.error("subscription_charge_foreign_payment", user_id=int(user.id))
        return
    await _resolve(deps, subscription, user, order, result.status)


async def _resolve(
    deps: Deps,
    subscription: Subscription,
    user: User,
    order: Payment,
    status: ChargeStatus,
) -> None:
    """Исход известен — или ещё нет."""
    if status is ChargeStatus.CHARGED:
        await _charged(deps, order.id, user)
    elif status is ChargeStatus.PENDING:
        await _retry_same(deps, subscription)
    else:
        # Явный отказ — единственный исход, после которого следующая попытка
        # вправе завести новый заказ.
        await deps.storage.release_charge_order(subscription.user_id, order.id)
        await _charge_failed(deps, subscription, user)


async def _retry_same(deps: Deps, subscription: Subscription) -> None:
    """Переспросить то же списание скоро — пока провайдер помнит ключ."""
    await deps.storage.advance_subscription(
        subscription.user_id,
        next_charge_at=deps.now()
        + timedelta(minutes=deps.settings.charge_unknown_retry_minutes),
        status=subscription.status,
        failed_since=subscription.failed_since,
        same_charge=True,
    )


# --- Вспомогательное -----------------------------------------------------


async def _charged(deps: Deps, order_id: str, user: User) -> None:
    """Деньги получены: выдаём период и говорим об этом.

    Выдача идёт тем же путём, что и первая оплата, — через ``confirm``. Это
    не экономия строк: там живут и атомарная отметка «оплачен», и продление
    срока, и запись согласия. Второй такой путь неизбежно разошёлся бы с
    первым.
    """
    confirmed = await payments.confirm(deps, order_id)
    if confirmed is None:
        return
    await payments.announce(deps, session_for(deps, user), confirmed, renewal=True)


async def _charge_failed(deps: Deps, subscription: Subscription, user: User) -> None:
    """Банк отказал. Три попытки с суточным шагом, потом конец (§4.16 оферты).

    Считаем не попытки, а окно: все попытки должны уложиться в
    ``charge_retry_days`` дней от первого отказа. Попытка, следующая за
    которой вышла бы за окно, — последняя. С настройками по умолчанию это
    ровно три: в день отказа, через сутки и через двое. Сбой провайдера сюда
    не попадает вовсе — он не отказ.
    """
    now = deps.now()
    failed_since = subscription.failed_since or now
    next_try = _retry_at(deps)
    if next_try >= failed_since + timedelta(days=deps.settings.charge_retry_days):
        await _end(deps, subscription, user)
        return

    if not await deps.storage.advance_subscription(
        subscription.user_id,
        next_charge_at=next_try,
        status=SubscriptionStatus.PAST_DUE.value,
        failed_since=failed_since,
    ):
        # Подписку отменили, пока мы ходили к банку. Ни переносить, ни
        # сообщать об отказе уже незачем.
        return
    deps.logger.info("subscription_charge_failed", user_id=int(user.id))

    screen = texts.subscription_charge_failed(
        subscription.tariff,
        amount=subscription.amount,
        currency=subscription.currency,
        next_try=texts.format_date(current_day(next_try, deps.settings.timezone)),
    )
    await deps.messenger.send_text(
        session_for(deps, user).chat,
        screen.text,
        keyboard=keyboards.subscription_manage(),
    )
    # Сообщение называет сумму и дату следующей попытки — это и есть
    # предупреждение о ней (§4.13). Отметка ставится после отправки, как и у
    # обычного напоминания: не дошло сообщение — повтор не пройдёт молча, а
    # перенесётся с новым предупреждением.
    await deps.storage.mark_reminded(subscription.user_id, next_try)


async def _await_stars(deps: Deps, subscription: Subscription, user: User) -> None:
    """Списание по звёздам делает Telegram — здесь мы только ждём его.

    Ждём с запасом: сообщение об оплате может задержаться, и объявлять
    подписку прекращённой из-за минутной задержки было бы враньём. Но и ждать
    вечно нельзя — иначе отменённая в Telegram подписка навсегда осталась бы
    у нас действующей.
    """
    if subscription.failed_since is None:
        await deps.storage.advance_subscription(
            subscription.user_id,
            next_charge_at=subscription.next_charge_at
            + timedelta(hours=deps.settings.stars_grace_hours),
            status=subscription.status,
            failed_since=deps.now(),
        )
        return

    deps.logger.info("subscription_stars_not_renewed", user_id=int(user.id))
    await _end(deps, subscription, user)


async def _end(deps: Deps, subscription: Subscription, user: User) -> None:
    """Прекращает подписку и говорит об этом (§4.16 оферты).

    Тариф при этом не трогаем: у него есть свой срок, и до его конца человек
    пользуется оплаченным. Дальше он кончится сам — этим занимается
    ``active_tariff``, а не мы.
    """
    await deps.storage.cancel_subscription(subscription.user_id, deps.now())
    deps.logger.info(
        "subscription_ended",
        user_id=int(user.id),
        tariff=subscription.tariff.value,
        method=subscription.method,
    )
    screen = texts.subscription_ended(subscription.tariff)
    await deps.messenger.send_text(
        session_for(deps, user).chat,
        screen.text,
        keyboard=keyboards.open_tariffs(),
    )


def _retry_at(deps: Deps) -> datetime:
    """Когда пробовать списание в следующий раз."""
    return deps.now() + timedelta(hours=deps.settings.charge_retry_hours)


def _until(deps: Deps, user: User, subscription: Subscription) -> str:
    """До какого числа работает оплаченное.

    Берём срок тарифа, а не дату списания: это разные вещи, и человеку важна
    первая. Если срока почему-то нет, ближайшее списание — лучшее, что мы
    можем честно назвать.
    """
    until = user.tariff_expires_at or subscription.next_charge_at
    return texts.format_date(current_day(until, deps.settings.timezone))


async def _say(deps: Deps, session: Session, screen: texts.Screen) -> None:
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.subscription_manage()
    )
