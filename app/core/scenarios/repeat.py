"""Повтор последнего действия и шеринг последнего результата.

Кнопки «🔄 Ещё раз», «Повторить», «📤 Поделиться» и «📤 Отправить другу» из
§2.2–2.4 работают через один и тот же запомненный контекст. Держать это в
одном месте, а не размазывать по сценариям, стоит того: логика «что именно
считать последним действием» одна, и меняться она должна одновременно для
всех кнопок.

Контекст один на пользователя, а кнопки остаются в переписке навсегда,
поэтому каждая говорит, чего именно она ждёт. Без этой проверки «Повторить»
под давней ошибкой чата перерисовала бы последнюю картинку — и списала бы за
неё лимит, которого человек не просил тратить.
"""

from __future__ import annotations

from app.core import retry_context, texts
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import chat, images, presets
from app.core.scenarios.deps import Deps, Session
from config import presets as registry


async def repeat_last(deps: Deps, session: Session, expected: RetryKind) -> None:
    """Повторяет последнее действие, если оно того же вида, что и кнопка."""
    context = _context(session, expected)
    if context is None:
        await _nothing_to_repeat(deps, session)
        return

    match context.kind:
        case RetryKind.CHAT:
            await chat.handle_message(deps, session, context.prompt)
        case RetryKind.IMAGE:
            await images.draw(deps, session, context.prompt)
        case RetryKind.PRESET:
            await _repeat_preset(deps, session, context)


async def share_last(deps: Deps, session: Session, expected: RetryKind) -> None:
    """Отправляет последнюю картинку с подписью и реферальной ссылкой."""
    context = _context(session, expected)
    if context is None or context.result_photo is None:
        await _nothing_to_repeat(deps, session)
        return

    await images.share_by_ref(deps, session, context.result_photo)


def _context(session: Session, expected: RetryKind) -> RetryContext | None:
    """Запомненный контекст, если он того же вида, что и нажатая кнопка."""
    context = retry_context.decode(session.user.retry_context)
    if context is None or context.kind is not expected:
        return None
    return context


async def _repeat_preset(deps: Deps, session: Session, context: RetryContext) -> None:
    """Применяет тот же прикол к тем же фото."""
    preset = (
        registry.PRESETS.get(context.preset_id)
        if context.preset_id is not None
        else None
    )
    if preset is None or len(context.source_photos) != preset.photos_required:
        # Пресет из контекста могли убрать из реестра между версиями или
        # переделать под другое число снимков. Отправлять человека в тупик
        # нельзя: показываем меню приколов, оттуда всё доступно.
        await presets.show_menu(deps, session)
        return

    photos = await presets.download_sources(deps, session, context.source_photos)
    if photos is None:
        # Ссылка у мессенджера протухла. Сценарий уже сказал об этом человеку
        # и показал, с чего начать заново, — падать тут нечему.
        return

    await presets.apply(deps, session, preset, photos, context.source_photos)


async def _nothing_to_repeat(deps: Deps, session: Session) -> None:
    """Повторять нечего — но и тупика быть не должно."""
    screen = texts.nothing_to_repeat()
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
