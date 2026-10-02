"""Раздел «Презентации» (фаза 10).

Путь человека: кнопка → тема словами → оформление из списка провайдера →
«Готовлю презентацию, около минуты» → PDF и PPTX → итог с кнопками.

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

Презентация по докладу собирается по тексту доклада, а он в базу не
кладётся вовсе: ни в ожидание, ни в «Повторить». Там лежит только жетон, а
сам текст — в памяти процесса несколько часов (порт Handoff). Устарел
жетон — кнопка честно говорит, что данных уже нет, и даёт выход.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from app.core import pending, retry_context, texts
from app.core.actions import Action
from app.core.generations import GenerationKind, error_code
from app.core.limits import LimitKind
from app.core.models import Document
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending, telemetry
from app.core.scenarios.deps import Deps, Session
from app.ports.handoff import Carried
from app.ports.presentations import BuiltPresentation, PresentationBusyError
from config.presentation_topics import SUGGESTED_TOPICS

#: Границы темы — те же, что у провайдера (docs/API.md §3.1).
MIN_TOPIC = 3
MAX_TOPIC = 200

#: Через сколько сборка считается брошенной. Пять минут ждём колоду (§8),
#: по минуте на каждый из двух файлов и запас на отправку: живая сборка
#: столько не длится, а умершая с процессом не должна запирать человека.
STALE_AFTER = timedelta(minutes=10)

#: Сколько текста доклада уходит провайдеру материалом. Он берёт в работу
#: первые 40 000 знаков (docs/API.md §3.1), остальное отбросил бы сам.
MATERIAL_LIMIT = 40_000

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
        keyboard=keyboards.presentation_ask(),
    )


async def suggest(deps: Deps, session: Session) -> None:
    """«Придумай сам»: тема из готового списка, и сразу выбор оформления.

    Ни к какому провайдеру за темой не ходим: список лежит в config/, и
    выбор из него бесплатный и мгновенный. Остаток проверяется заново —
    кнопка могла прийти из старого сообщения.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return

    topic = _pick(deps)
    await _offer_themes(
        deps,
        session,
        awaiting=pending.await_presentation_theme(topic),
        remember=RetryContext(kind=RetryKind.PRESENTATION, prompt=topic),
        shown_topic=topic,
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

    await _offer_themes(
        deps,
        session,
        awaiting=pending.await_presentation_theme(topic),
        remember=RetryContext(kind=RetryKind.PRESENTATION, prompt=topic),
    )


async def from_report(deps: Deps, session: Session, token: str) -> None:
    """«Сделать презентацию по докладу»: сразу к выбору оформления.

    Жетон здесь только проверяется, а забирается при нажатии на оформление:
    до сборки дело может и не дойти, и сжигать кнопку раньше времени
    незачем. Остаток — до жетона: при пустом пейволл, кнопка не сгорает.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return

    if deps.handoff is None or deps.handoff.peek(token) is None:
        await _link_gone(deps, session, token)
        return

    await _offer_themes(
        deps,
        session,
        awaiting=pending.await_presentation_source(token),
        remember=RetryContext(kind=RetryKind.PRESENTATION, source=token),
    )


async def choose_theme(deps: Deps, session: Session, theme_id: str) -> None:
    """Нажато оформление — собираем.

    Ожидание берётся из базы заново, а не из снимка сессии. Второе нажатие
    той же кнопки после готовой колоды найдёт его уже снятым — и колоду не
    соберёт, а спросит новую тему.
    """
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    fresh = await deps.storage.get_user_by_id(session.user.id) or session.user
    source = pending.parse_await_presentation_source(fresh.pending)
    if source is not None:
        await _build_from(deps, session, source, theme_id)
        return

    topic = pending.parse_await_presentation_theme(fresh.pending)
    if topic is None:
        if _building(deps, fresh.presentation_started_at):
            # Второе нажатие, пока первая колода собирается: ожидание она уже
            # сняла. Спросить тему заново значило бы сбить человека с толку.
            await _say_in_progress(deps, session)
            return
        await start(deps, session)
        return

    await _build(deps, session, topic, theme_id)


async def retry(deps: Deps, session: Session) -> None:
    """«Повторить» под сбоем: та же тема (или тот же доклад) и оформление."""
    if deps.presentations is None:
        await _unavailable(deps, session)
        return

    context = retry_context.decode(session.user.retry_context)
    if context is None or context.kind is not RetryKind.PRESENTATION:
        await deps.messenger.send_text(
            session.chat, texts.nothing_to_repeat().text, show_menu=True
        )
        return

    if context.source is not None:
        if context.theme_id is None:
            await from_report(deps, session, context.source)
        else:
            await _build_from(deps, session, context.source, context.theme_id)
        return

    if context.theme_id is None:
        # Упал список оформлений, а не сборка: показываем его снова.
        await _offer_themes(
            deps,
            session,
            awaiting=pending.await_presentation_theme(context.prompt),
            remember=context,
        )
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


async def _say_in_progress(deps: Deps, session: Session) -> None:
    await deps.messenger.send_text(
        session.chat, texts.presentation_in_progress().text, show_menu=False
    )


async def _link_gone(deps: Deps, session: Session, token: str) -> None:
    """Под кнопкой-связкой данных нет: уже сделано или устарело. Выход есть."""
    used = deps.handoff is not None and deps.handoff.was_taken(token)
    screen = (
        texts.link_already_used(texts.MENU_PRESENTATIONS)
        if used
        else texts.link_expired_presentation()
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.link_exit(
            texts.MENU_PRESENTATIONS, Action.MENU_PRESENTATIONS
        ),
    )


def _pick(deps: Deps) -> str:
    """Тема из списка. По часам, а не случайно: в тестах часы стоят, и
    выбор повторяем, а человеку разница незаметна."""
    moment = int(deps.now().timestamp() * 1000)
    return SUGGESTED_TOPICS[moment % len(SUGGESTED_TOPICS)]


async def _offer_themes(
    deps: Deps,
    session: Session,
    *,
    awaiting: str,
    remember: RetryContext,
    shown_topic: str | None = None,
) -> None:
    """Список оформлений — из API, не зашитый у нас.

    ``awaiting`` — ожидание, которое встанет на время выбора; ``remember`` —
    что повторить, если сам список не загрузился.
    """
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
        await deps.storage.set_retry_context(
            session.user.id, replace(remember, theme_id=None).encode()
        )
        await deps.storage.set_pending(session.user.id, None)
        await deps.messenger.send_text(
            session.chat,
            texts.presentation_error().text,
            keyboard=keyboards.presentation_retry(),
        )
        return

    await deps.storage.set_pending(session.user.id, awaiting)
    choices = tuple((theme.name, theme.id) for theme in themes)
    names = tuple(name for name, _ in choices)
    screen = (
        texts.presentation_suggested(shown_topic, names)
        if shown_topic is not None
        else texts.presentation_pick_theme(names)
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.presentation_themes(choices)
    )


async def _build_from(deps: Deps, session: Session, token: str, theme_id: str) -> None:
    """Презентация по докладу: жетон забирается, не вышло — возвращается."""
    handoff = deps.handoff
    carried = handoff.take(token) if handoff is not None else None
    if carried is None:
        fresh = await deps.storage.get_user_by_id(session.user.id) or session.user
        if _building(deps, fresh.presentation_started_at):
            await _say_in_progress(deps, session)
            return
        await _link_gone(deps, session, token)
        return

    assert handoff is not None
    delivered = False
    try:
        delivered = await _build(
            deps,
            session,
            carried.topic,
            theme_id,
            material=carried.material,
            source=token,
        )
    finally:
        if not delivered:
            handoff.give_back(token, carried)


async def _build(
    deps: Deps,
    session: Session,
    topic: str,
    theme_id: str,
    *,
    material: str = "",
    source: str | None = None,
) -> bool:
    """Проверка остатка, захват слота — и сборка под ним. True — доставлено."""
    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return False

    if not await deps.storage.claim_presentation(
        session.user.id, deps.now(), stale_after=STALE_AFTER
    ):
        await _say_in_progress(deps, session)
        return False

    try:
        return await _build_claimed(
            deps, session, topic, theme_id, material=material, source=source
        )
    finally:
        await deps.storage.release_presentation(session.user.id)


async def _build_claimed(
    deps: Deps,
    session: Session,
    topic: str,
    theme_id: str,
    *,
    material: str,
    source: str | None,
) -> bool:
    """Собрать, доставить, списать — в этом порядке и только в нём."""
    assert deps.presentations is not None
    # Ожидание снимаем до обращения к провайдеру: сборка может упасть, а
    # следующее сообщение не должно приклеиться к прошлой теме. Повтор от
    # этого не страдает — что повторить, уже в контексте «Повторить».
    await deps.storage.set_pending(session.user.id, None)
    # Тему по докладу в базу не кладём: она из его текста. Повтор найдёт её
    # по жетону.
    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESENTATION,
            prompt=topic if source is None else "",
            theme_id=theme_id,
            source=source,
        ).encode(),
    )

    waiting = await deps.messenger.send_text(
        session.chat, texts.presentation_working().text, show_menu=False
    )
    deps.logger.info(
        "presentation_started",
        user_id=int(session.user.id),
        theme=theme_id,
        from_report=source is not None,
    )

    started = deps.now()
    try:
        built = await deps.presentations.build(
            topic, theme_id=theme_id, material=material
        )
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
        return False
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
        return False

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
        return False

    # Файлы у человека — это и есть доставка. Списываем до сообщений после
    # них: их сбой не отменяет того, что презентацию уже получили.
    await spending.charge(deps, session, LimitKind.PRESENTATIONS)
    deps.logger.info(
        "presentation_delivered",
        user_id=int(session.user.id),
        with_pdf=built.pdf is not None,
    )
    await deps.messenger.edit_text(waiting, texts.presentation_done().text)
    # Итог — отдельным сообщением после файлов, а не правкой «готовлю»: то
    # стоит над файлами, а кнопки «что дальше» нужны под ними.
    report_token = (
        deps.handoff.put(Carried(topic=topic)) if deps.handoff is not None else None
    )
    await deps.messenger.send_text(
        session.chat,
        texts.presentation_result(with_pdf=built.pdf is not None).text,
        keyboard=keyboards.presentation_result(report_token),
    )
    return True


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
