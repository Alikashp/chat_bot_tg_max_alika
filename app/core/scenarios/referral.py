"""Экран «🎁 Моя ссылка» (§2.7).

Отдаём готовое сообщение для пересылки, а не голую ссылку. Разница
принципиальная: со ссылкой человеку надо придумать, что к ней написать, и
большинство просто не станет. С готовым сообщением остаётся одно действие —
«Переслать».
"""

from __future__ import annotations

from app.core import texts
from app.core.referral import referral_url
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session


async def show_offer(deps: Deps, session: Session) -> None:
    """Объясняет, что человек получит за друга, и даёт одну кнопку.

    Сначала выгода, потом ссылка. Голая ссылка не объясняет, зачем её
    пересылать, — а «Позови друга, тебе +50 сообщений» объясняет.
    """
    screen = texts.referral_offer(
        bonus_messages=deps.settings.referral_bonus_messages,
        bonus_images=deps.settings.referral_bonus_images,
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.referral_offer()
    )


async def send_invitation(deps: Deps, session: Session) -> None:
    """Присылает готовое к пересылке приглашение.

    Отдельным сообщением и без клавиатуры: его пересылают целиком, и всё
    лишнее в нём поедет к другу вместе с текстом.
    """
    url = referral_url(
        deps.settings.referral_link_host,
        deps.settings.bot_username,
        session.user.referral_code,
    )
    screen = texts.referral_invite(url)
    await deps.messenger.send_text(session.chat, screen.text, show_menu=False)
