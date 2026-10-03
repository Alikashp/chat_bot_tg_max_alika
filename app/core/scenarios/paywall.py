"""Экран исчерпания (§2.5).

Показывается только при исчерпании лимита и никогда на входе: пейволл на
первом экране убивает конверсию у человека, который ещё не понял, зачем ему
этот бот.

Две кнопки, и обе — выход. Тупика быть не должно никогда.
"""

from __future__ import annotations

from app.core import texts
from app.core.limits import LimitKind
from app.core.scenarios import channel, keyboards
from app.core.scenarios.deps import Deps, Session


async def show(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Показывает пейволл по исчерпанному виду лимита.

    Числа наград и текст первой строки собираются здесь из одних и тех же
    настроек, что и подписи кнопок: экран и клавиатура обязаны обещать одно и
    то же, иначе человек прочтёт «+2», а нажмёт «+5».
    """
    if kind is LimitKind.MESSAGES:
        bonus = deps.settings.referral_bonus_messages
        screen = texts.paywall_messages(invite_messages=bonus)
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.paywall(texts.button_invite_for_messages(bonus)),
        )
        return

    if kind is LimitKind.PRESENTATIONS:
        bonus = deps.settings.referral_bonus_presentations
        screen = texts.paywall_presentations(bonus)
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.paywall_presentations(
                texts.button_invite_for_presentations(bonus)
            ),
        )
        return

    if kind is LimitKind.DOCUMENTS:
        screen = texts.paywall_documents(
            # Доклады по суткам не возобновляются ни на одном тарифе: у них
            # месячная норма, и «завтра будет ещё» было бы обманом.
            renews_tomorrow=False,
        )
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.paywall(),
        )
        return

    bonus = deps.settings.referral_bonus_images
    for_channel = channel.available(deps, session)
    screen = texts.paywall_images(
        # Картинки по суткам не возобновляются ни на одном тарифе: у них
        # месячная норма, и «завтра будет ещё» было бы обманом.
        renews_tomorrow=False,
        invite_images=bonus,
        channel_images=for_channel,
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.paywall(
            texts.button_invite_for_images(bonus),
            texts.button_channel_bonus(for_channel) if for_channel else "",
        ),
    )
