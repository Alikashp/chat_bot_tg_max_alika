"""Картинки по описанию (§2.3).

Нажал «🎨 Картинки» → описал → получил результат.

Сообщение «Рисую… ~15 сек» не остаётся в переписке мусором: оно заменяется
самой картинкой. Отдельная строчка «готово» после ожидания — это лишнее
сообщение, которое человеку придётся проматывать.
"""

from __future__ import annotations

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import Photo
from app.core.referral import referral_url
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session


async def ask_for_description(deps: Deps, session: Session) -> None:
    """Спрашивает, что нарисовать."""
    screen = texts.image_ask()
    await deps.messenger.send_text(session.chat, screen.text)


async def draw(deps: Deps, session: Session, description: str) -> None:
    """Рисует картинку по описанию."""
    allowance = await spending.current_allowance(deps, session, LimitKind.IMAGES)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.IMAGES)
        return

    waiting = await deps.messenger.send_text(
        session.chat, texts.image_drawing().text, show_menu=False
    )

    try:
        photo = await deps.images.generate(
            description, quality=session.tariff.image_quality
        )
    except Exception as error:
        deps.logger.warning(
            "image_failed", user_id=int(session.user.id), error=repr(error)
        )
        await deps.messenger.edit_text(
            waiting,
            texts.image_error().text,
            keyboard=keyboards.retry(Action.IMAGE_RETRY),
        )
        return

    await deps.messenger.edit_to_photo(
        waiting, photo, keyboard=keyboards.image_result()
    )

    # Картинка доставлена — только теперь списываем.
    await spending.charge(deps, session, LimitKind.IMAGES)


async def share(deps: Deps, session: Session, photo: Photo) -> None:
    """«📤 Поделиться»: картинка с подписью и персональной ссылкой (§2.3).

    Ссылка здесь не украшение: это единственный виральный канал, встроенный
    прямо в результат, которым и так хочется похвастаться.
    """
    url = referral_url(deps.settings.bot_username, session.user.referral_code)
    screen = texts.share_caption(deps.settings.bot_username, url)
    await deps.messenger.send_photo(session.chat, photo, caption=screen.text)
