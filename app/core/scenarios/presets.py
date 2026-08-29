"""Пресеты обработки фото — главный виральный крючок (§2.4).

Один шаг: кинул фото → получил результат. Никаких «выберите стиль» и
«уточните детали».

Пресет здесь — запись в реестре config/presets.py, а не отдельный обработчик.
Добавление третьего пресета не требует ни строчки в этом файле: он приходит
в меню, в разбор нажатия и в обработку фото сам (критерий приёмки A1).
"""

from __future__ import annotations

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import Photo
from app.core.photos import PhotoProblem, check_photo
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session
from config import presets as registry
from config.presets import Preset

#: Что показать пользователю по каждой причине отказа.
_REJECTION_TEXTS: dict[PhotoProblem, str] = {
    PhotoProblem.TOO_BIG: texts.PHOTO_TOO_BIG,
    PhotoProblem.NOT_AN_IMAGE: texts.PHOTO_NOT_AN_IMAGE,
}


async def show_menu(deps: Deps, session: Session) -> None:
    """Показывает список приколов — прямо из реестра."""
    screen = texts.presets_menu(tuple(p.button for p in registry.PRESETS.values()))
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.presets_menu(
            tuple((preset.button, preset.id) for preset in registry.PRESETS.values())
        ),
    )


async def ask_for_photo(deps: Deps, session: Session, preset: Preset) -> None:
    """Просит прислать фото под выбранный прикол."""
    screen = texts.preset_ask_photo(preset.invitation)
    await deps.messenger.send_text(session.chat, screen.text)


async def apply(
    deps: Deps,
    session: Session,
    preset: Preset,
    photo: Photo,
    source_ref: str | None = None,
) -> None:
    """Обрабатывает присланное фото выбранным пресетом.

    ``source_ref`` — ссылка на исходное фото у мессенджера. Нужна кнопке
    «🔄 Ещё раз»: чтобы применить прикол заново, надо знать, к чему его
    применяли, а сами байты хранить у себя незачем — они уже лежат там.
    """
    check = check_photo(photo, max_bytes=deps.settings.max_photo_bytes)
    if check.problem is not None:
        # Отказываем до обращения к провайдеру (§3.5): и деньги целее, и
        # чужой формат не поедет в чужой разбор.
        screen = texts.photo_rejected(_REJECTION_TEXTS[check.problem])
        await deps.messenger.send_text(session.chat, screen.text)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.IMAGES)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.IMAGES)
        return

    waiting = await deps.messenger.send_text(
        session.chat, texts.preset_working().text, show_menu=False
    )

    try:
        result = await deps.images.edit(
            photo, preset.instruction, quality=session.tariff.image_quality
        )
    except Exception as error:
        deps.logger.warning(
            "preset_failed",
            user_id=int(session.user.id),
            preset=preset.id,
            error=repr(error),
        )
        await deps.storage.set_retry_context(
            session.user.id,
            RetryContext(
                kind=RetryKind.PRESET,
                preset_id=preset.id,
                source_photo=source_ref,
            ).encode(),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.preset_error().text,
            keyboard=keyboards.retry(Action.PRESET_RETRY),
        )
        return

    delivered = await deps.messenger.edit_to_photo(
        waiting, result, keyboard=keyboards.preset_result()
    )
    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESET,
            preset_id=preset.id,
            source_photo=source_ref,
            result_photo=delivered,
        ).encode(),
    )
    await spending.charge(deps, session, LimitKind.IMAGES)
