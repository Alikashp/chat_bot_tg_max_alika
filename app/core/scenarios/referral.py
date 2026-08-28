"""Экран «🎁 Моя ссылка» (§2.7).

Отдаём готовое сообщение для пересылки, а не голую ссылку. Разница
принципиальная: со ссылкой человеку надо придумать, что к ней написать, и
большинство просто не станет. С готовым сообщением остаётся одно действие —
«Переслать».
"""

from __future__ import annotations

from app.core import texts
from app.core.referral import referral_url
from app.core.scenarios.deps import Deps, Session


async def show_link(deps: Deps, session: Session) -> None:
    """Присылает готовое к пересылке приглашение."""
    url = referral_url(deps.settings.bot_username, session.user.referral_code)
    screen = texts.referral_invite(url)
    await deps.messenger.send_text(session.chat, screen.text, show_menu=False)
