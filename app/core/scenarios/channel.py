"""Разовый бонус за подписку на канал.

Третья ступень бесплатной лестницы: три картинки при регистрации, по две за
каждого приглашённого друга и две — один раз — за подписку на канал. Дальше
тарифы.

Бонус выдаётся ровно один раз и только после настоящей проверки. Верить
нажатию кнопки нельзя: подписаться, забрать картинки и отписаться — это одно
движение, и повторять его можно было бы бесконечно.
"""

from __future__ import annotations

from app.core import texts
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session


def available(deps: Deps, session: Session) -> int:
    """Сколько картинок человеку ещё положено за канал. 0 — предлагать нечего.

    Нечего в трёх случаях: канал не настроен, проверять подписку в этом
    мессенджере нечем (в MAX своего канала у нас нет) или бонус уже выдан.
    """
    if deps.channel is None or not deps.settings.channel_url:
        return 0
    if session.user.channel_bonus_at is not None:
        return 0
    return deps.settings.channel_bonus_images


async def show_offer(deps: Deps, session: Session) -> None:
    """Объясняет, за что дают картинки, и даёт уйти в канал."""
    bonus = available(deps, session)
    if not bonus:
        await _say_already_taken(deps, session)
        return

    screen = texts.channel_offer(bonus_images=bonus)
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.channel_offer(deps.settings.channel_url),
    )


async def check(deps: Deps, session: Session) -> None:
    """Проверяет подписку и начисляет бонус.

    Пользователь перечитывается из хранилища: между показом предложения и
    нажатием проверки бонус мог уже уехать — например, человек нажал кнопку
    дважды с двух устройств.
    """
    if deps.channel is None or not deps.settings.channel_url:
        # Кнопка из времени, когда канал был настроен. Тупика быть не должно.
        await _say_already_taken(deps, session)
        return

    user = await deps.storage.get_user_by_id(session.user.id) or session.user
    if user.channel_bonus_at is not None:
        await _say_already_taken(deps, session)
        return

    try:
        subscribed = await deps.channel.has_member(user.external_id)
    except Exception as error:
        # «Не смогли проверить» и «не подписан» — разные вещи. Свалить одно
        # в другое значит не выдать заслуженный бонус и оставить человека с
        # ощущением, что его обманули.
        deps.logger.warning(
            "channel_check_failed", user_id=int(user.id), error=repr(error)
        )
        screen = texts.channel_check_failed()
        await deps.messenger.send_text(
            session.chat, screen.text, keyboard=keyboards.channel_retry()
        )
        return

    if not subscribed:
        screen = texts.channel_not_subscribed()
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.channel_offer(deps.settings.channel_url),
        )
        return

    bonus = deps.settings.channel_bonus_images
    if not await deps.storage.grant_channel_bonus(user.id, images=bonus):
        # Успели начислить параллельно. Второй раз не даём.
        await _say_already_taken(deps, session)
        return

    deps.logger.info("channel_bonus_granted", user_id=int(user.id), images=bonus)
    granted = texts.channel_granted(bonus_images=bonus)
    await deps.messenger.send_text(session.chat, granted.text, show_menu=True)


async def _say_already_taken(deps: Deps, session: Session) -> None:
    """Бонус разовый. Но уйти с экрана должно быть куда."""
    invite = deps.settings.referral_bonus_images
    screen = texts.channel_already_taken(invite_images=invite)
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.paywall(texts.button_invite_for_images(invite)),
    )
