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

    bonus = deps.settings.referral_bonus_images
    for_channel = channel.available(deps, session)
    screen = texts.paywall_images(
        # На бесплатном тарифе дневной нормы картинок нет, и «завтра будет
        # ещё» там было бы обманом: завтра не будет ничего.
        renews_tomorrow=session.tariff.daily_images > 0,
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
