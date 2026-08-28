"""Экран исчерпания (§2.5).

Показывается только при исчерпании лимита и никогда на входе: пейволл на
первом экране убивает конверсию у человека, который ещё не понял, зачем ему
этот бот.

Две кнопки, и обе — выход. Тупика быть не должно никогда.
"""

from __future__ import annotations

from app.core import texts
from app.core.limits import LimitKind
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session


async def show(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Показывает пейволл по исчерпанному виду лимита."""
    if kind is LimitKind.IMAGES:
        screen = texts.paywall_images()
        invite_label = texts.BUTTON_INVITE_FOR_IMAGES
    else:
        screen = texts.paywall_messages()
        invite_label = texts.BUTTON_INVITE_FOR_MESSAGES

    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.paywall(invite_label)
    )
