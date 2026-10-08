"""Экран исчерпания (§2.5).

Показывается только при исчерпании лимита и никогда на входе: пейволл на
первом экране убивает конверсию у человека, который ещё не понял, зачем ему
этот бот.

Две кнопки, и обе — выход. Тупика быть не должно никогда.
"""

from __future__ import annotations

from app.core import texts
from app.core.limits import LimitKind
from app.core.scenarios import keyboards, spending
from app.core.scenarios.deps import Deps, Session


async def show(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Показывает пейволл по исчерпанному виду лимита.

    Числа наград и текст первой строки собираются здесь из одних и тех же
    настроек, что и подписи кнопок: экран и клавиатура обязаны обещать одно и
    то же, иначе человек прочтёт «+2», а нажмёт «+5».

    У картинок, докладов и презентаций — месячная норма, и пейволл говорит,
    когда придёт новая, только если она правда придёт (Т7).
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

    renewal = await spending.renewal(deps, session, kind)
    renews_on = texts.format_date(renewal[0]) if renewal is not None else None
    by_charge = renewal is not None and renewal[1]

    if kind is LimitKind.PRESENTATIONS:
        bonus = deps.settings.referral_bonus_presentations
        screen = texts.paywall_presentations(
            bonus, renews_on=renews_on, by_charge=by_charge
        )
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.paywall(texts.button_invite_for_presentations(bonus)),
        )
        return

    if kind is LimitKind.DOCUMENTS:
        screen = texts.paywall_documents(renews_on=renews_on, by_charge=by_charge)
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.paywall(),
        )
        return

    bonus = deps.settings.referral_bonus_images
    screen = texts.paywall_images(
        renews_on=renews_on, by_charge=by_charge, invite_images=bonus
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.paywall(texts.button_invite_for_images(bonus)),
    )
