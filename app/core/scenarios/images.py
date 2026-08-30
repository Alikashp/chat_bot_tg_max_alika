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
from app.core.referral import referral_url
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session
from app.ports.ai import ContentRefusedError


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
    except ContentRefusedError as refusal:
        # Провайдер ответил по существу: такое он рисовать не станет. Это не
        # сбой, и предлагать «Повторить» нельзя — повтор даст тот же отказ, а
        # кнопка обещала бы обратное. Контекст повтора тоже не сохраняем.
        deps.logger.info(
            "image_refused", user_id=int(session.user.id), reason=str(refusal)
        )
        await deps.messenger.edit_text(waiting, texts.image_refused().text)
        return
    except Exception as error:
        deps.logger.warning(
            "image_failed", user_id=int(session.user.id), error=repr(error)
        )
        await deps.storage.set_retry_context(
            session.user.id,
            RetryContext(kind=RetryKind.IMAGE, prompt=description).encode(),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.image_error().text,
            keyboard=keyboards.retry(Action.IMAGE_RETRY),
        )
        return

    delivered = await deps.messenger.edit_to_photo(
        waiting, photo, keyboard=keyboards.image_result()
    )

    # Картинка доставлена — только теперь списываем.
    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.IMAGE, prompt=description, result_photo=delivered
        ).encode(),
    )
    await spending.charge(deps, session, LimitKind.IMAGES)


async def share_by_ref(deps: Deps, session: Session, photo_ref: str) -> None:
    """Отправляет картинку с подписью и персональной ссылкой (§2.3).

    Ссылка здесь не украшение: это единственный виральный канал, встроенный
    прямо в результат, которым и так хочется похвастаться.

    Пересылаем по ссылке мессенджера, а не байтами: картинка у него уже
    лежит, и заливать её повторно ради подписи было бы лишними секундами
    ожидания на ровном месте.
    """
    await deps.messenger.send_photo_by_ref(
        session.chat, photo_ref, caption=_share_caption(deps, session)
    )


def _share_caption(deps: Deps, session: Session) -> str:
    url = referral_url(
        deps.settings.referral_link_host,
        deps.settings.bot_username,
        session.user.referral_code,
    )
    return texts.share_caption(deps.settings.bot_username, url).text
