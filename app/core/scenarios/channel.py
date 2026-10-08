"""Обязательная подписка на канал (сессия 8, шаг 3).

Включается настройкой и действует только в Telegram: в MAX своего канала у
нас нет. Бесплатный неподписанный человек не получает ни одного платного для
нас результата — чат, картинки, приколы, доклады и презентации закрыты
экраном подписки. Приветствие, профиль, тарифы, оплата и отключение
продления открыты всегда: за них мы не платим, а закрыть дорогу к оплате
значило бы запереть человека, который хочет от проверки откупиться.

Платящих проверка не касается — включая пробный период: тариф у них не
бесплатный. Подтверждение Telegram помнится настроенный срок (по умолчанию
десять минут): спрашивать на каждое сообщение незачем, а отписавшийся
остановится не позже. Отказ не помнится — подписавшийся проходит сразу.
Не дал Telegram проверить — человека пропускаем: «не смогли проверить» и «не
подписан» — разные вещи.

Разовый бонус +2 картинки за подписку убран вместе с кнопкой на пейволле;
уже начисленные картинки остаются на балансе.
"""

from __future__ import annotations

from app.core import pending, texts
from app.core.actions import (
    Action,
    parse_buy_action,
    parse_email_action,
    parse_method_action,
)
from app.core.generations import error_code
from app.core.models import IncomingMessage, MessengerKind, TariffId
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session

#: Что открыто всегда. Остальное закрыто: новая кнопка, которую забудут
#: сюда вписать, окажется под проверкой, а не бесплатной в обход неё.
#: Кроме названного заказчиком — бесплатная навигация: меню, «Поделиться»
#: уже сделанным, приглашение друга, новый диалог и сама проверка.
_OPEN_ACTIONS = frozenset(
    {
        Action.MENU_PROFILE,
        Action.MENU_TARIFFS,
        Action.OPEN_TARIFFS,
        Action.TRIAL,
        Action.SUBSCRIPTION,
        Action.SUBSCRIPTION_OFF,
        Action.MENU_SHOW,
        Action.CHANNEL_CHECK,
        Action.CHANNEL_OFFER,
        Action.INVITE_FRIEND,
        Action.MY_LINK,
        Action.REFERRAL_SEND,
        Action.CHAT_NEW_DIALOG,
        Action.IMAGE_SHARE,
        Action.PRESET_SHARE,
    }
)


def active(deps: Deps, session: Session) -> bool:
    """Касается ли проверка этого человека прямо сейчас. Без обращений наружу."""
    if not deps.settings.channel_required or not deps.settings.channel_url:
        return False
    if deps.channel is None or session.chat.messenger is not MessengerKind.TELEGRAM:
        return False
    return session.tariff.id is TariffId.FREE


def is_paid_work(
    session: Session, incoming: IncomingMessage, action: str | None
) -> bool:
    """Ведёт ли обращение к платному для нас результату."""
    if incoming.paid_order_id is not None:
        return False
    if action is not None:
        return not _is_open(action)
    if incoming.photo_ref is not None or incoming.document_ref is not None:
        return True
    if incoming.text:
        # Почта для чека — середина оплаты, а не вопрос в чат.
        return pending.parse_await_email(session.user.pending) is None
    return False


def _is_open(action: str) -> bool:
    return (
        action in _OPEN_ACTIONS
        or parse_buy_action(action) is not None
        or parse_method_action(action) is not None
        or parse_email_action(action) is not None
    )


async def allows(deps: Deps, session: Session) -> bool:
    """Пропускает подписанного; неподписанному показывает экран подписки.

    Ничего не списывается: проверка стоит до сценария, и за остановленное
    ею сообщение человек не платит.
    """
    checked = session.user.channel_checked_at
    if checked is not None and session.now - checked < deps.settings.channel_check_ttl:
        return True

    subscribed = await _subscribed(deps, session)
    if subscribed is not False:
        return True

    deps.logger.info("channel_gate_shown", user_id=int(session.user.id))
    screen = texts.channel_required()
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.channel_required(deps.settings.channel_url),
    )
    return False


async def check(deps: Deps, session: Session) -> None:
    """«✅ Я подписался»: проверяет и пускает дальше — без /start."""
    if not active(deps, session):
        # Кнопка из старой переписки или проверка уже не касается человека.
        await show_menu(deps, session)
        return

    if await _subscribed(deps, session) is False:
        screen = texts.channel_not_subscribed()
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.channel_required(deps.settings.channel_url),
        )
        return

    screen = texts.channel_passed()
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)


async def show_menu(deps: Deps, session: Session) -> None:
    """Меню отдельным сообщением: выход для кнопок, которым больше нечего делать."""
    on = deps.presentations_on
    await deps.messenger.send_text(
        session.chat,
        texts.menu(keyboards.menu_labels(presentations=on)).text,
        keyboard=keyboards.main_menu(presentations=on),
        show_menu=False,
    )


async def _subscribed(deps: Deps, session: Session) -> bool | None:
    """Спрашивает Telegram. None — проверить не дали; тогда пропускаем.

    Подтверждение запоминается, отказ — нет: подписавшийся минуту назад
    должен пройти сразу, не дожидаясь, пока забудется прошлый ответ.
    """
    assert deps.channel is not None
    try:
        subscribed = await deps.channel.has_member(session.user.external_id)
    except Exception as error:
        # Причину отказа Telegram адаптер уже записал (channel_check_refused);
        # здесь — что человека пропустили.
        deps.logger.warning(
            "channel_gate_skipped",
            user_id=int(session.user.id),
            error=error_code(error),
        )
        return None
    if subscribed:
        await deps.storage.remember_channel_check(session.user.id, session.now)
    return subscribed
