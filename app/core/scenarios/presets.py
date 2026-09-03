"""Пресеты обработки фото — главный виральный крючок (§2.4).

Один шаг: кинул фото → получил результат. Никаких «выберите стиль» и
«уточните детали». Исключение ровно одно и оно в самом приколе: «я и я в
детстве» соединяет два снимка, и второй просто неоткуда взять, кроме как
попросить.

Пресет здесь — запись в реестре config/presets.py, а не отдельный обработчик.
Добавление пресета не требует ни строчки в этом файле: он приходит в меню, в
разбор нажатия и в обработку фото сам (критерий приёмки A1). Замок и число
нужных фото — тоже поля записи, а не условия по идентификатору.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import Photo
from app.core.pending import await_preset
from app.core.photos import PhotoProblem, check_photo
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session
from app.ports.ai import ContentRefusedError
from config import presets as registry
from config.presets import Preset

#: Что показать пользователю по каждой причине отказа.
_REJECTION_TEXTS: dict[PhotoProblem, str] = {
    PhotoProblem.TOO_BIG: texts.PHOTO_TOO_BIG,
    PhotoProblem.NOT_AN_IMAGE: texts.PHOTO_NOT_AN_IMAGE,
}


def _is_locked(preset: Preset, session: Session) -> bool:
    """Закрыт ли прикол для этого человека прямо сейчас.

    Тариф берётся действующий, а не записанный: у оплаченного есть срок, и
    после него замок должен вернуться на место сам.
    """
    return preset.paid_only and session.tariff.is_free


def _preset_buttons(session: Session) -> tuple[str, ...]:
    """Подписи приколов в порядке реестра, с замком на закрытых."""
    return tuple(
        texts.locked_button(preset.button)
        if _is_locked(preset, session)
        else preset.button
        for preset in registry.PRESETS.values()
    )


def _preset_choices(session: Session) -> tuple[tuple[str, str], ...]:
    """Пары «подпись, идентификатор» для клавиатуры.

    Закрытые приколы остаются в списке и остаются нажимаемыми: замок — это
    витрина, а не забор. Нажатие на него ведёт к тарифам, то есть ровно туда,
    зачем он в списке и стоит.
    """
    return tuple(
        (label, preset.id)
        for label, preset in zip(
            _preset_buttons(session), registry.PRESETS.values(), strict=True
        )
    )


async def show_menu(deps: Deps, session: Session) -> None:
    """Показывает список приколов — прямо из реестра."""
    screen = texts.presets_menu(_preset_buttons(session))
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.presets_menu(_preset_choices(session)),
    )


async def pick(deps: Deps, session: Session, preset: Preset) -> None:
    """Выбран прикол: проверяем замок, запоминаем и просим первое фото."""
    if await _refuse_locked(deps, session, preset):
        return

    await deps.storage.set_pending(session.user.id, await_preset(preset.id))
    await _ask_for_photo(deps, session, preset, collected=0)


async def add_photo(
    deps: Deps,
    session: Session,
    preset: Preset,
    photo: Photo,
    photo_ref: str,
    collected: tuple[str, ...] = (),
) -> None:
    """Принимает очередное фото под выбранный прикол.

    ``collected`` — ссылки на снимки, присланные раньше под этот же прикол.
    Пока их не набралось столько, сколько просит реестр, работа не начинается:
    просим следующее фото и запоминаем то, что уже есть.

    Байты только что присланного снимка передаются сюда готовыми — их уже
    скачал маршрутизатор. Ранние снимки скачиваются здесь заново: между двумя
    обращениями прошло время, и держать чужие байты у себя всё это время
    незачем.
    """
    if await _reject_unsuitable(deps, session, (photo,)):
        # Ожидание не трогаем: человек просто присылает другой снимок
        # вместо этого, и переспрашивать на каждом было бы глупо.
        return

    refs = (*collected, photo_ref)
    if len(refs) < preset.photos_required:
        # Остаток проверяем до того, как просить следующий снимок: у кого
        # картинки кончились, тот иначе прислал бы второе фото впустую.
        allowance = await spending.current_allowance(deps, session, LimitKind.IMAGES)
        if allowance.exhausted:
            await deps.storage.set_pending(session.user.id, None)
            await paywall.show(deps, session, LimitKind.IMAGES)
            return

        await deps.storage.set_pending(session.user.id, await_preset(preset.id, refs))
        await _ask_for_photo(deps, session, preset, collected=len(refs))
        return

    earlier = await download_sources(deps, session, collected)
    if earlier is None:
        return

    if collected:
        # Пара собрана — забываем её до всякой обработки. Ожидание при этом
        # остаётся: человек может прислать подряд ещё одну пару, и
        # переспрашивать про прикол было бы глупо. А вот приклеить его
        # следующий снимок к уже отработанному первому нельзя ни в каком
        # случае: он получил бы прошлое фото в новом результате.
        #
        # Именно до обработки, а не после: она может упасть, и тогда
        # отработанный снимок остался бы ждать напарника.
        await deps.storage.set_pending(session.user.id, await_preset(preset.id))

    await apply(deps, session, preset, (*earlier, photo), refs)


async def apply(
    deps: Deps,
    session: Session,
    preset: Preset,
    photos: Sequence[Photo],
    refs: Sequence[str] = (),
) -> None:
    """Обрабатывает собранные фото выбранным приколом.

    ``refs`` — ссылки на исходники у мессенджера. Нужны кнопке «🔄 Ещё раз»:
    чтобы применить прикол заново, надо знать, к чему его применяли, а сами
    байты хранить у себя незачем — они уже лежат там.
    """
    # Обе проверки стоят на единственной двери к провайдеру, а не только там,
    # где фото пришло от человека: снимок мог доехать сюда и по кнопке
    # «Ещё раз», и ни один из путей не должен уметь их обойти.
    #
    # Замок здесь — не перестраховка. Кнопка «🔄 Ещё раз» остаётся в переписке
    # навсегда: без этой проверки человек, у которого подписка кончилась,
    # нажимал бы её под своей прошлогодней фигуркой и получал бы новую.
    if await _refuse_locked(deps, session, preset):
        return
    if await _reject_unsuitable(deps, session, photos):  # §3.5
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
            photos, preset.instruction, quality=session.tariff.image_quality
        )
    except ContentRefusedError as refusal:
        # Отказ по содержанию: дело в самом фото, и повтор ничего не изменит.
        # Выход с экрана — список приколов, чтобы человек не остался ни с чем.
        deps.logger.info(
            "preset_refused",
            user_id=int(session.user.id),
            preset=preset.id,
            reason=str(refusal),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.preset_refused(_preset_buttons(session)).text,
            keyboard=keyboards.presets_menu(_preset_choices(session)),
        )
        return
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
                source_photos=tuple(refs),
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
            source_photos=tuple(refs),
            result_photo=delivered,
        ).encode(),
    )
    await spending.charge(deps, session, LimitKind.IMAGES)


async def _refuse_locked(deps: Deps, session: Session, preset: Preset) -> bool:
    """Показывает замок и возвращает True, если прикол ещё не открыт.

    Момент, когда человек сам показал, за что готов заплатить. Пейволл
    исчерпания здесь не подходит: лимит цел, дело не в нём.
    """
    if not _is_locked(preset, session):
        return False

    deps.logger.info("preset_locked", user_id=int(session.user.id), preset=preset.id)
    await deps.messenger.send_text(
        session.chat,
        texts.preset_locked().text,
        keyboard=keyboards.preset_locked(),
    )
    return True


async def _reject_unsuitable(
    deps: Deps, session: Session, photos: Sequence[Photo]
) -> bool:
    """Говорит о негодном фото и возвращает True, если работать нельзя.

    Отказ бесплатный: он случается до обращения к провайдеру (§3.5) — и
    деньги целее, и чужой формат не поедет в чужой разбор.
    """
    for photo in photos:
        check = check_photo(photo, max_bytes=deps.settings.max_photo_bytes)
        if check.problem is None:
            continue
        screen = texts.photo_rejected(_REJECTION_TEXTS[check.problem])
        await deps.messenger.send_text(session.chat, screen.text)
        return True
    return False


async def _ask_for_photo(
    deps: Deps, session: Session, preset: Preset, *, collected: int
) -> None:
    """Просит очередное фото. Слова берутся из реестра пресетов."""
    screen = texts.preset_ask_photo(
        preset.invitations[collected], cancellable=collected > 0
    )
    keyboard = keyboards.preset_cancel() if collected > 0 else None
    await deps.messenger.send_text(session.chat, screen.text, keyboard=keyboard)


async def download_sources(
    deps: Deps, session: Session, refs: Sequence[str]
) -> list[Photo] | None:
    """Забирает у мессенджера присланные раньше снимки. None — не вышло.

    В MAX ссылка на фото живёт не вечно, а между первым снимком и вторым —
    и тем более между результатом и нажатием «🔄 Ещё раз» неделю спустя —
    человек может уйти надолго. Тогда обрабатывать нечего, и говорить об
    ошибке обработки было бы неправдой: начинаем сбор заново.

    Публичная не для красоты: тем же путём ходит повтор по кнопке, и
    оставить его без этой обработки значило бы уронить обработчик молча —
    человек не получил бы вообще ничего.
    """
    photos: list[Photo] = []
    for ref in refs:
        try:
            photos.append(
                await deps.messenger.download_photo(
                    ref, max_bytes=deps.settings.max_photo_bytes
                )
            )
        except Exception as error:
            deps.logger.warning(
                "preset_source_lost", user_id=int(session.user.id), error=repr(error)
            )
            await deps.storage.set_pending(session.user.id, None)
            await deps.messenger.send_text(
                session.chat,
                texts.preset_photo_lost(_preset_buttons(session)).text,
                keyboard=keyboards.presets_menu(_preset_choices(session)),
            )
            return None
    return photos
