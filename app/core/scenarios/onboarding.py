"""Первый экран и разбор deeplink (§2.1, §2.7).

Три строки и клавиатура. Никаких списков возможностей на пятнадцать пунктов:
человек пришёл попробовать, а не читать инструкцию.

Здесь же разбираются обе ветки deeplink — акция бота презентаций и
реферальная ссылка. Разбор именно тут, а не в адаптере, потому что правила
одинаковы для Telegram и MAX: в MAX у события запуска бота есть ровно такое
же поле payload (docs/research.md §1.5).
"""

from __future__ import annotations

from datetime import timedelta

from app.core import referral, texts
from app.core.models import Chat, MessengerKind, User
from app.core.scenarios.deps import Deps, Session

#: Сколько попыток подобрать незанятый реферальный код. Коллизия на 10^12
#: вариантов маловероятна, но «маловероятно» и «невозможно» — разные вещи,
#: а падение на регистрации стоит нам пользователя.
_CODE_ATTEMPTS = 5

#: Окно, за которое считается суточный лимит наград (§2.7).
_REWARD_WINDOW = timedelta(days=1)


async def start(
    deps: Deps,
    chat: Chat,
    messenger: MessengerKind,
    external_id: str,
    payload: str = "",
) -> Session:
    """Обрабатывает /start и возвращает сессию.

    Для уже знакомого пользователя просто показывает первый экран заново:
    награды за deeplink положены только новым (§2.7, антифрод).
    """
    existing = await deps.storage.get_user(messenger, external_id)
    if existing is not None:
        session = Session(user=existing, chat=chat, day=deps.today())
        await _greet(deps, session, from_presentations=False, gifted=False)
        return session

    from_presentations = referral.is_from_presentations(payload)
    user = await _create_user(
        deps, messenger, external_id, from_presentations=from_presentations
    )
    session = Session(user=user, chat=chat, day=deps.today())

    gifted = await _apply_referral(deps, session, payload)
    if gifted:
        # Бонус уже начислен — перечитываем, чтобы первый же экран показывал
        # правду, а не то, что было до подарка.
        refreshed = await deps.storage.get_user_by_id(user.id)
        if refreshed is not None:
            session = Session(user=refreshed, chat=chat, day=session.day)

    await _greet(deps, session, from_presentations=from_presentations, gifted=gifted)
    return session


async def _create_user(
    deps: Deps,
    messenger: MessengerKind,
    external_id: str,
    *,
    from_presentations: bool,
) -> User:
    """Заводит пользователя, подбирая свободный реферальный код."""
    quota = (
        deps.settings.presentation_daily_images
        if from_presentations
        else deps.settings.free_daily_images
    )
    last_error: ValueError | None = None
    for _ in range(_CODE_ATTEMPTS):
        try:
            return await deps.storage.create_user(
                messenger=messenger,
                external_id=external_id,
                referral_code=referral.generate_code(),
                daily_image_quota=quota,
            )
        except ValueError as error:
            last_error = error
    raise RuntimeError("не удалось подобрать свободный реферальный код") from last_error


async def _apply_referral(deps: Deps, session: Session, payload: str) -> bool:
    """Начисляет награду обоим, если ссылка настоящая. Возвращает, был ли подарок.

    Идемпотентность обеспечивает хранилище: повторный /start по той же ссылке
    физически не может записать пару дважды. Здесь остаётся только суточный
    потолок на одного пригласившего.
    """
    code = referral.parse_referral_payload(payload)
    if code is None:
        return False

    referrer = await deps.storage.get_user_by_referral_code(code)
    if referrer is None or referrer.id == session.user.id:
        # Несуществующий код или ссылка на самого себя. Про self-referral
        # хранилище знает и само, но лишний запрос делать незачем.
        return False

    since = deps.now() - _REWARD_WINDOW
    rewarded_today = await deps.storage.count_referrals_since(referrer.id, since)
    if rewarded_today >= deps.settings.referral_daily_reward_limit:
        deps.logger.warning(
            "referral_limit_reached",
            user_id=int(referrer.id),
            rewarded_today=rewarded_today,
        )
        return False

    if not await deps.storage.record_referral(referrer.id, session.user.id):
        return False

    bonus_messages = deps.settings.referral_bonus_messages
    bonus_images = deps.settings.referral_bonus_images
    await deps.storage.add_bonus(
        referrer.id, messages=bonus_messages, images=bonus_images
    )
    await deps.storage.add_bonus(
        session.user.id, messages=bonus_messages, images=bonus_images
    )

    await _notify_referrer(deps, referrer)
    return True


async def _notify_referrer(deps: Deps, referrer: User) -> None:
    """Сообщает пригласившему, что друг зашёл (§2.7).

    Сбой отправки не должен отменять уже начисленную награду: бонус на месте,
    человек увидит его в профиле, а падать здесь значило бы уронить онбординг
    приглашённому из-за проблемы у пригласившего.
    """
    screen = texts.referral_reward()
    try:
        await deps.messenger.send_text(
            Chat(messenger=referrer.messenger, chat_id=referrer.external_id),
            screen.text,
            show_menu=True,
        )
    except Exception as error:
        deps.logger.warning(
            "referral_notice_failed",
            user_id=int(referrer.id),
            error=repr(error),
        )


async def _greet(
    deps: Deps, session: Session, *, from_presentations: bool, gifted: bool
) -> None:
    screen = texts.onboarding(
        daily_messages=session.tariff.daily_messages,
        daily_images=session.user.daily_image_quota,
        from_presentations=from_presentations,
        referral_gift=gifted,
    )
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
