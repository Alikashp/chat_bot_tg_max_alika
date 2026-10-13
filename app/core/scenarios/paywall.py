"""Экран исчерпания (§2.5).

Показывается только при исчерпании лимита и никогда на входе: пейволл на
первом экране убивает конверсию у человека, который ещё не понял, зачем ему
этот бот.

Две кнопки, и обе — выход. Тупика быть не должно никогда.
"""

from __future__ import annotations

from app.core import texts
from app.core.actions import Action, buy_action
from app.core.limits import LimitKind
from app.core.models import TariffId
from app.core.scenarios import keyboards, payments, spending
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import TRIAL, tariff_of


async def show(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Показывает пейволл по исчерпанному виду лимита.

    Числа наград и текст первой строки собираются здесь из одних и тех же
    настроек, что и подписи кнопок: экран и клавиатура обязаны обещать одно и
    то же, иначе человек прочтёт «+2», а нажмёт «+5».

    У картинок, докладов и презентаций — месячная норма, и пейволл говорит,
    когда придёт новая, только если она правда придёт (Т7).

    Бесплатному — не список тарифов, а одно предложение Лайта (сессия 11);
    платящим — прежние экраны.
    """
    if session.tariff.is_free:
        await _offer_lite(deps, session, kind)
        return

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


async def _offer_lite(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Пейволл бесплатного тарифа: Лайт за 1 ₽ на пробу или за свою цену.

    Пробный период — только тому, кому он сейчас положен (ни разу не
    платил, включён, у магазина этого мессенджера есть автоплатежи). Вторая
    кнопка — бесплатный способ добрать этот же ресурс, если он есть: у
    докладов его нет.
    """
    trial = TRIAL if await payments.trial_offered(deps, session) else None
    lite = tariff_of(TariffId.LITE)
    screen = texts.paywall_free(kind, lite=lite, trial=trial)
    action = Action.PAYWALL_TRIAL if trial is not None else buy_action(lite.id.value)
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.paywall_free(
            texts.button_continue(lite=lite, trial=trial),
            action,
            _free_way(deps, kind),
        ),
    )


def _free_way(deps: Deps, kind: LimitKind) -> str:
    """Подпись кнопки «позвать друга» для этого ресурса; пусто — способа нет."""
    if kind is LimitKind.MESSAGES:
        return texts.button_invite_for_messages(deps.settings.referral_bonus_messages)
    if kind is LimitKind.IMAGES:
        return texts.button_invite_for_images(deps.settings.referral_bonus_images)
    if kind is LimitKind.PRESENTATIONS:
        return texts.button_invite_for_presentations(
            deps.settings.referral_bonus_presentations
        )
    return ""
