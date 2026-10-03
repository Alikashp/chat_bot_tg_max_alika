"""Раздел «Презентации» (фаза 10).

Путь человека: кнопка → тема словами (или «Придумай сам», или кнопка под
докладом) → экран параметров, уже заполненный → «Собрать презентацию» →
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

**Темы нет ни в базе, ни в логах, ни в учёте (сессия 7, В6).** Всё, что
показывает экран параметров, — тема, текст доклада, параметры — лежит в
черновике в памяти процесса несколько часов (``core/decks.py``, порт
Tokens). В базе — только жетон черновика: в ожидании новой темы и в
контексте «Повторить». Устарел жетон — кнопка честно говорит, что данных
уже нет, и даёт выход.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from app.core import decks, pending, retry_context, texts
from app.core.actions import Action, DeckAction, DeckField
from app.core.decks import Draft, with_theme
from app.core.generations import GenerationKind, error_code
from app.core.limits import LimitKind
from app.core.models import Document
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending, telemetry
from app.core.scenarios.deps import Deps, Session
from app.ports.handoff import Carried
from app.ports.presentations import (
    AUDIENCES,
    LANGUAGES,
    MAX_SLIDES,
    MIN_SLIDES,
    BuiltPresentation,
    PresentationBusyError,
    PresentationTheme,
)
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
    if not await _ready(deps, session):
        return

    await deps.storage.set_pending(session.user.id, pending.AWAIT_PRESENTATION_TOPIC)
    await deps.messenger.send_text(
        session.chat,
        texts.presentation_ask().text,
        keyboard=keyboards.presentation_ask(),
    )


async def suggest(deps: Deps, session: Session) -> None:
    """«Придумай сам»: тема из готового списка — и сразу экран параметров.

    Ни к какому провайдеру за темой не ходим: список лежит в config/, и
    выбор из него бесплатный и мгновенный. Остаток проверяется заново —
    кнопка могла прийти из старого сообщения.
    """
    if not await _ready(deps, session):
        return
    await _open_screen(deps, session, Draft(topic=_pick(deps)))


async def receive_topic(deps: Deps, session: Session, written: str) -> None:
    """Пришла тема словами: новая колода или новая тема для экрана.

    Тема вне границ отклоняется до всякого обращения к провайдеру (К3).
    Ожидание при этом остаётся: человек просто напишет её ещё раз.
    """
    if deps.presentations is None or deps.drafts is None:
        await _unavailable(deps, session)
        return

    token = pending.parse_await_deck_topic(session.user.pending)
    topic = normalise_topic(written)
    if topic is None:
        await deps.messenger.send_text(
            session.chat,
            texts.presentation_topic_bad().text,
            keyboard=(
                keyboards.deck_back(token)
                if token is not None
                else keyboards.presentation_cancel()
            ),
        )
        return

    if token is None:
        await _open_screen(deps, session, Draft(topic=topic))
        return

    draft = deps.drafts.peek(token)
    if draft is None:
        await deps.storage.set_pending(session.user.id, None)
        await _gone(deps, session)
        return
    deps.drafts.update(token, replace(draft, topic=topic))
    await show_screen(deps, session, token)


async def from_report(deps: Deps, session: Session, token: str) -> None:
    """«Сделать презентацию по докладу»: экран параметров с докладом-материалом.

    Жетон доклада здесь только читается, а забирается, когда колода
    доставлена: до сборки дело может и не дойти, и сжигать кнопку раньше
    времени незачем. Остаток — до жетона: при пустом пейволл.
    """
    if not await _ready(deps, session):
        return

    carried = deps.handoff.peek(token) if deps.handoff is not None else None
    if carried is None:
        await _link_gone(deps, session, token)
        return

    await _open_screen(
        deps,
        session,
        Draft(topic=carried.topic, material=carried.material, report_token=token),
    )


async def show_screen(deps: Deps, session: Session, token: str) -> None:
    """Экран параметров по черновику: заполнен, собирает одной кнопкой (В2)."""
    assert deps.drafts is not None
    draft = deps.drafts.peek(token)
    if draft is None:
        await _gone(deps, session)
        return

    themes = await _themes(deps, session, token)
    if themes is None:
        return
    draft = with_theme(draft, themes)
    deps.drafts.update(token, draft)
    await deps.storage.set_pending(session.user.id, None)

    names = {theme.id: theme.name for theme in themes}
    screen = texts.deck_screen(
        topic=draft.topic,
        language=draft.language,
        slides=draft.slides,
        audience=draft.audience,
        design=names[draft.theme_id],
        file_name=draft.file.filename if draft.file is not None else None,
        from_report=bool(draft.material),
    )
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.deck_screen(token)
    )


async def act(deps: Deps, session: Session, action: DeckAction) -> None:
    """Кнопка экрана параметров: собрать, выбрать, задать, вернуться."""
    if deps.presentations is None or deps.drafts is None:
        await _unavailable(deps, session)
        return

    draft = deps.drafts.peek(action.token)
    if draft is None:
        if action.kind == "go" and deps.drafts.was_taken(action.token):
            await _say(deps, session, texts.deck_already_built())
            return
        await _gone(deps, session)
        return

    if action.kind == "go":
        await _build_draft(deps, session, action.token, draft)
        return
    if action.kind == "back" or action.field is None:
        await show_screen(deps, session, action.token)
        return
    if action.kind == "pick":
        await _pick_value(deps, session, action.token, draft, action.field)
        return
    await _set_value(deps, session, action.token, draft, action.field, action.value)


async def choose_theme(deps: Deps, session: Session, theme_id: str) -> None:
    """Кнопка оформления из версии до экрана параметров (В5).

    Тему такие кнопки брали из ожидания в базе, а там её больше нет.
    Отвечаем честно и ведём начать заново.
    """
    await _gone(deps, session)


async def retry(deps: Deps, session: Session) -> None:
    """«Повторить» под сбоем: тот же черновик — экран или сборка."""
    if deps.presentations is None or deps.drafts is None:
        await _unavailable(deps, session)
        return

    context = retry_context.decode(session.user.retry_context)
    if (
        context is None
        or context.kind is not RetryKind.PRESENTATION
        or context.source is None
    ):
        await deps.messenger.send_text(
            session.chat, texts.nothing_to_repeat().text, show_menu=True
        )
        return

    draft = deps.drafts.peek(context.source)
    if draft is None:
        await _gone(deps, session)
        return
    if context.theme_id is None:
        # Упал список оформлений, а не сборка: показываем экран снова.
        await show_screen(deps, session, context.source)
        return
    await _build_draft(deps, session, context.source, draft)


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


async def _ready(deps: Deps, session: Session) -> bool:
    """Раздел включён и презентации у человека есть; иначе — ответ и False."""
    if deps.presentations is None or deps.drafts is None:
        await _unavailable(deps, session)
        return False
    allowance = await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.PRESENTATIONS)
        return False
    return True


async def _open_screen(deps: Deps, session: Session, draft: Draft) -> None:
    """Заводит черновик в памяти и показывает по нему экран."""
    assert deps.drafts is not None
    token = deps.drafts.put(draft)
    await show_screen(deps, session, token)


async def _themes(
    deps: Deps, session: Session, token: str
) -> tuple[PresentationTheme, ...] | None:
    """Оформления из API; None — не загрузились, человеку сказано и дан повтор."""
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
    if themes:
        return themes

    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(kind=RetryKind.PRESENTATION, source=token).encode(),
    )
    await deps.storage.set_pending(session.user.id, None)
    await deps.messenger.send_text(
        session.chat,
        texts.presentation_error().text,
        keyboard=keyboards.presentation_retry(),
    )
    return None


async def _pick_value(
    deps: Deps, session: Session, token: str, draft: Draft, field: DeckField
) -> None:
    """Показывает варианты одного параметра — или спрашивает новую тему."""
    if field is DeckField.TOPIC:
        await deps.storage.set_pending(session.user.id, pending.await_deck_topic(token))
        await deps.messenger.send_text(
            session.chat,
            texts.deck_new_topic().text,
            keyboard=keyboards.deck_back(token),
        )
        return

    if field is DeckField.DESIGN:
        themes = await _themes(deps, session, token)
        if themes is None:
            return
        question = texts.DECK_ASK_DESIGN
        options = tuple((theme.name, theme.id) for theme in themes)
        current, per_row = draft.theme_id, 1
    elif field is DeckField.LANGUAGE:
        question = texts.DECK_ASK_LANGUAGE
        options = tuple((texts.LANGUAGE_LABELS[code], code) for code in LANGUAGES)
        current, per_row = draft.language, 2
    elif field is DeckField.AUDIENCE:
        question = texts.DECK_ASK_AUDIENCE
        options = tuple((texts.AUDIENCE_LABELS[code], code) for code in AUDIENCES)
        current, per_row = draft.audience, 2
    elif field is DeckField.SLIDES:
        question = texts.DECK_ASK_SLIDES
        options = tuple(
            (str(count), str(count)) for count in range(MIN_SLIDES, MAX_SLIDES + 1)
        )
        current, per_row = str(draft.slides), 6
    else:
        await show_screen(deps, session, token)
        return

    screen = texts.deck_pick(question, tuple(label for label, _ in options))
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.deck_options(
            token, field, options, current=current, per_row=per_row
        ),
    )


async def _set_value(
    deps: Deps,
    session: Session,
    token: str,
    draft: Draft,
    field: DeckField,
    value: str,
) -> None:
    """Задаёт значение и возвращает на экран. Чужое значение — не меняет ничего.

    Значение приходит из данных кнопки, то есть снаружи: проверяется по
    спискам API, а оформление — по списку от самого провайдера.
    """
    changed: Draft | None = None
    if field is DeckField.LANGUAGE:
        changed = decks.with_language(draft, value)
    elif field is DeckField.AUDIENCE:
        changed = decks.with_audience(draft, value)
    elif field is DeckField.SLIDES:
        changed = decks.with_slides(draft, value)
    elif field is DeckField.DESIGN:
        themes = await _themes(deps, session, token)
        if themes is None:
            return
        changed = decks.with_design(draft, value, themes)
    if changed is not None:
        assert deps.drafts is not None
        deps.drafts.update(token, changed)
    await show_screen(deps, session, token)


async def _build_draft(deps: Deps, session: Session, token: str, draft: Draft) -> None:
    """Собирает по черновику. Доставлено — черновик и жетон доклада забираются.

    Забираются только после доставки: упавшая сборка оставляет черновик
    на месте, и «Повторить» соберёт по нему же. Второе нажатие «Собрать»
    после готовой колоды найдёт черновик взятым и скажет, что всё уже
    собрано.
    """
    if not draft.theme_id:
        # Черновик ни разу не показывался с оформлением — показываем.
        await show_screen(deps, session, token)
        return
    if await _build(deps, session, token, draft):
        assert deps.drafts is not None
        deps.drafts.take(token)
        if draft.report_token is not None and deps.handoff is not None:
            deps.handoff.take(draft.report_token)


async def _gone(deps: Deps, session: Session) -> None:
    """Под кнопкой экрана параметров данных нет: устарела или перезапуск (В5)."""
    await _say(deps, session, texts.deck_gone())


async def _say(deps: Deps, session: Session, screen: texts.Screen) -> None:
    """Короткий ответ с выходом «Презентации»."""
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.link_exit(
            texts.MENU_PRESENTATIONS, Action.MENU_PRESENTATIONS
        ),
    )


async def _build(deps: Deps, session: Session, token: str, draft: Draft) -> bool:
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
        return await _build_claimed(deps, session, token, draft)
    finally:
        await deps.storage.release_presentation(session.user.id)


async def _build_claimed(
    deps: Deps, session: Session, token: str, draft: Draft
) -> bool:
    """Собрать, доставить, списать — в этом порядке и только в нём."""
    assert deps.presentations is not None
    # Ожидание снимаем до обращения к провайдеру: сборка может упасть, а
    # следующее сообщение не должно приклеиться к прошлой теме. Повтор от
    # этого не страдает — что повторить, уже в контексте «Повторить».
    await deps.storage.set_pending(session.user.id, None)
    # Темы в базе нет (В6): «Повторить» найдёт черновик по жетону. Отметка
    # оформления говорит, что повторять надо сборку, а не показ экрана.
    theme_id = draft.theme_id
    await deps.storage.set_retry_context(
        session.user.id,
        RetryContext(
            kind=RetryKind.PRESENTATION, theme_id=theme_id, source=token
        ).encode(),
    )

    waiting = await deps.messenger.send_text(
        session.chat, texts.presentation_working().text, show_menu=False
    )
    deps.logger.info(
        "presentation_started",
        user_id=int(session.user.id),
        theme=theme_id,
        from_report=bool(draft.material),
        with_file=draft.file is not None,
        language=draft.language,
        slides=draft.slides,
        audience=draft.audience,
    )

    started = deps.now()
    try:
        built = await deps.presentations.build(draft.request())
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
        for document in _documents(draft.topic, built):
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
        deps.handoff.put(Carried(topic=draft.topic))
        if deps.handoff is not None
        else None
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
