"""Раздел «Презентации» (фаза 10).

Путь человека: кнопка → тема словами → оформление из списка провайдера →
«Готовлю презентацию, около минуты» → PDF и PPTX → «Ещё одну».

Три правила, ради которых файл написан так, а не короче.

**Списание — только после доставки.** Провайдер собрал колоду, но файл до
человека не доехал — презентация не списана. Сбой API, ожидание дольше пяти
минут, сбой отправки: человек видит «не получилось» и «Повторить». PDF не
собрался, а PPTX доехал — это доставка: PPTX полноценный файл.

**Одна сборка на человека.** Слот занимается в базе одним условным UPDATE
(``claim_presentation``), а не ограничителем в памяти: у ограничителя предел
настраивается, а «одна колода за раз» — правило продукта, не настройка.
Повторно доставленное обновление и двойное нажатие упираются в занятый слот
и вторую колоду не создают.

**Темы нет в логах и в учёте.** Она лежит только там, где без неё нельзя:
в ожидании выбора оформления и в контексте «Повторить» — в базе, рядом с
описаниями картинок, которые там хранятся по той же причине.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.core import pending, retry_context, texts
from app.core.generations import GenerationKind, error_code
from app.core.limits import LimitKind
from app.core.models import Document
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending, telemetry
from app.core.scenarios.deps import Deps, Session
from app.ports.presentations import BuiltPresentation, PresentationBusyError

#: Границы темы — те же, что у провайдера (docs/API.md §3.1).
MIN_TOPIC = 3
MAX_TOPIC = 200

#: Через сколько сборка считается брошенной. Пять минут ждём колоду (§8),
#: по минуте на каждый из двух файлов и запас на отправку: живая сборка
#: столько не длится, а умершая с процессом не должна запирать человека.
STALE_AFTER = timedelta(minutes=10)

#: Что пишется в учёт как «модель». Провайдер собирает один вид колоды —
#: доклад, — и в учёте это и стоит назвать.
MODEL = "fibonacci:doklad"

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PDF_MIME = "application/pdf"

#: Чего не бывает в именах файлов ни в одной системе, куда их скачают.
_NOT_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: Длина имени файла. Тема бывает в двести знаков, а имя такой длины на
#: телефоне обрезается посередине и читается хуже короткого.
_FILENAME_LENGTH = 60


async def start(deps: Deps, session: Session) -> None:
    """Кнопка «Презентации» и «Ещё одну»: спрашиваем тему.

    Остаток проверяется здесь, до темы: иначе человек написал бы тему,
    выбрал оформление и только тогда узнал, что презентаций у него нет.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return

    await deps.storage.set_pending(session.user.id, pending.AWAIT_PRESENTATION_TOPIC)
    await deps.messenger.send_text(
        session.chat,
        texts.presentation_ask().text,
        keyboard=keyboards.presentation_cancel(),
    )


async def receive_topic(deps: Deps, session: Session, written: str) -> None:
    """Пришла тема словами — проверяем её и предлагаем оформления.

    Тема вне границ отклоняется до всякого обращения к провайдеру (К3).
    Ожидание темы при этом остаётся: человек просто напишет её ещё раз.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    topic = normalise_topic(written)
    if topic is None:
        await deps.storage.set_pending(
            session.user.id, pending.AWAIT_PRESENTATION_TOPIC
        )
        await deps.messenger.send_text(
            session.chat,
            texts.presentation_topic_bad().text,
            keyboard=keyboards.presentation_cancel(),
        )
        return

    await _offer_themes(deps, session, topic)


async def choose_theme(deps: Deps, session: Session, theme_id: str) -> None:
    """Нажато оформление — собираем.

    Тема берётся из базы заново, а не из снимка сессии. Второе нажатие той
    же кнопки после готовой колоды найдёт ожидание уже снятым — и колоду не
    соберёт, а спросит новую тему.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    fresh = await deps.storage.get_user_by_id(session.user.id) or session.user
    topic = pending.parse_await_presentation_theme(fresh.pending)
    if topic is None:
        if _building(deps, fresh.presentation_started_at):
            # Второе нажатие, пока первая колода собирается: ожидание она уже
            # сняла. Спросить тему заново значило бы сбить человека с толку.
            await deps.messenger.send_text(
                session.chat, texts.presentation_in_progress().text, show_menu=False
            )
            return
        await start(deps, session)
        return

    await _build(deps, session, topic, theme_id)


async def retry(deps: Deps, session: Session) -> None:
    """«Повторить» под сбоем: та же тема и то же оформление."""
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    context = retry_context.decode(session.user.retry_context)
    if context is None or context.kind is not RetryKind.PRESENTATION:
        await deps.messenger.send_text(
            session.chat, texts.nothing_to_repeat().text, show_menu=True
        )
        return

    if context.theme_id is None:
        # Упал список оформлений, а не сборка: показываем его снова.
        await _offer_themes(deps, session, context.prompt)
        return

    await _build(deps, session, context.prompt, context.theme_id)


def normalise_topic(written: str) -> str | None:
    """Тема в том виде, в каком уйдёт провайдеру; None — вне границ.

    Пробелы и переводы строк схлопываются: тема становится заголовком
    титульного слайда дословно, и абзац посреди заголовка ломает вёрстку.
    """
    topic = " ".join(written.split())
    if not MIN_TOPIC <= len(topic) <= MAX_TOPIC:
        return None
    return topic


def filename_for(topic: str) -> str:
    """Имя файла без расширения: тема, очищенная от запрещённых символов."""
    cleaned = " ".join(_NOT_IN_FILENAME.sub(" ", topic).split())
    cleaned = cleaned[:_FILENAME_LENGTH].strip(" .")
    return cleaned or texts.PRESENTATION_FILENAME


# --- Вспомогательное -----------------------------------------------------


def _building(deps: Deps, started_at: datetime | None) -> bool:
    """Идёт ли у человека сборка прямо сейчас (брошенная не в счёт)."""
    return started_at is not None and started_at >= deps.now() - STALE_AFTER


async def _offer_themes(deps: Deps, session: Session, topic: str) -> None:
    """Список оформлений — из API, не зашитый у нас."""
    assert deps.presentations is not None
    try:
        themes = await deps.presentations.themes()
    except Exception as error:
        deps.logger.warning(
            "presentation_themes_failed",
            user_id=int(session.user.id),
            error=error_code(error),
        )
        themes = ()

    if not themes:
        await _remember(deps, session, topic, theme_id=None)
        await deps.storage.set_pending(session.user.id, None)
        await deps.messenger.send_text(
            session.chat,
            texts.presentation_error().text,
            keyboard=keyboards.presentation_retry(),
        )
        return

    await deps.storage.set_pending(
        session.user.id, pending.await_presentation_theme(topic)
    )
    choices = tuple((theme.name, theme.id) for theme in themes)
    await deps.messenger.send_text(
        session.chat,
        texts.presentation_pick_theme(tuple(name for name, _ in choices)).text,
        keyboard=keyboards.presentation_themes(choices),
    )


async def _build(deps: Deps, session: Session, topic: str, theme_id: str) -> None:
    """Проверка остатка, захват слота — и сборка под ним."""
    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return

    if not await deps.storage.claim_presentation(
        session.user.id, deps.now(), stale_after=STALE_AFTER
    ):
        await deps.messenger.send_text(
            session.chat, texts.presentation_in_progress().text, show_menu=False
        )
        return

    try:
        await _build_claimed(deps, session, topic, theme_id)
    finally:
        await deps.storage.release_presentation(session.user.id)


async def _build_claimed(
    deps: Deps, session: Session, topic: str, theme_id: str
) -> None:
    """Собрать, доставить, списать — в этом порядке и только в нём."""
    assert deps.presentations is not None
    # Ожидание снимаем до обращения к провайдеру: сборка может упасть, а
    # следующее сообщение не должно приклеиться к прошлой теме. Повтор от
    # этого не страдает — тема и оформление уже в контексте «Повторить».
    await deps.storage.set_pending(session.user.id, None)
    await _remember(deps, session, topic, theme_id=theme_id)

    waiting = await deps.messenger.send_text(
        session.chat, texts.presentation_working().text, show_menu=False
    )
    deps.logger.info(
        "presentation_started", user_id=int(session.user.id), theme=theme_id
    )

    started = deps.now()
    try:
        built = await deps.presentations.build(topic, theme_id=theme_id)
    except PresentationBusyError as busy:
        if busy.reached_api:
            await _record(deps, session, theme_id, started=started, error=busy)
        deps.logger.warning(
            "presentation_busy",
            user_id=int(session.user.id),
            reached_api=busy.reached_api,
        )
        await deps.messenger.edit_text(
            waiting,
            texts.presentation_busy().text,
            keyboard=keyboards.presentation_retry(),
        )
        return
    except Exception as error:
        await _record(deps, session, theme_id, started=started, error=error)
        deps.logger.warning(
            "presentation_failed",
            user_id=int(session.user.id),
            error=error_code(error),
            code=getattr(error, "code", ""),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.presentation_error().text,
            keyboard=keyboards.presentation_retry(),
        )
        return

    await _record(deps, session, theme_id, started=started)

    try:
        for document in _documents(topic, built):
            await deps.messenger.send_document(session.chat, document)
    except Exception as error:
        # Колода собрана, но до человека не доехала. Он её не получил —
        # значит, и не платит; «Повторить» соберёт новую.
        deps.logger.warning(
            "presentation_delivery_failed",
            user_id=int(session.user.id),
            error=error_code(error),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.presentation_error().text,
            keyboard=keyboards.presentation_retry(),
        )
        return

    # Файлы у человека — это и есть доставка. Списываем до правки сообщения
    # «готовлю»: её сбой не отменяет того, что презентацию уже получили.
    await spending.charge(deps, session, LimitKind.PRESENTATIONS)
    deps.logger.info(
        "presentation_delivered",
        user_id=int(session.user.id),
        with_pdf=built.pdf is not None,
    )
    await deps.messenger.edit_text(
        waiting,
        texts.presentation_ready(with_pdf=built.pdf is not None).text,
        keyboard=keyboards.presentation_ready(),
    )


def _documents(topic: str, built: BuiltPresentation) -> tuple[Document, ...]:
    """Файлы в порядке отправки: PDF, если собрался, потом PPTX.

    PDF первым — его открывают посмотреть, PPTX — чтобы править, и второй
    файл ложится ниже, ближе к тому, что человек сделает дальше.
    """
    name = filename_for(topic)
    pptx = Document(data=built.pptx, filename=f"{name}.pptx", mime_type=_PPTX_MIME)
    if built.pdf is None:
        return (pptx,)
    pdf = Document(data=built.pdf, filename=f"{name}.pdf", mime_type=_PDF_MIME)
    return (pdf, pptx)


async def _remember(
    deps: Deps, session: Session, topic: str, *, theme_id: str | None
) -> None:
    """Запоминает, что повторить по кнопке «Повторить»."""
    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESENTATION, prompt=topic, theme_id=theme_id
        ).encode(),
    )


async def _record(
    deps: Deps,
    session: Session,
    theme_id: str,
    *,
    started: datetime,
    error: BaseException | None = None,
) -> None:
    """Учёт одной сборки (К9). Оформление — в колонке прикола, темы нет."""
    if error is None:
        await telemetry.record_success(
            deps,
            session,
            GenerationKind.PRESENTATION,
            started=started,
            model=MODEL,
            preset_id=theme_id,
        )
        return
    await telemetry.record_failure(
        deps,
        session,
        GenerationKind.PRESENTATION,
        started=started,
        model=MODEL,
        error=error,
        preset_id=theme_id,
    )


async def _unavailable(deps: Deps, session: Session) -> None:
    """Кнопка из меню, пришедшего до снятия ключа API. Тупика быть не должно."""
    await deps.messenger.send_text(
        session.chat, texts.unsupported_input().text, show_menu=True
    )
