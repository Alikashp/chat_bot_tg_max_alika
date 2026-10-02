"""Раздел «Документы»: прочитать присланный файл и отдать готовый.

Один круг: выбрал действие → кинул файл → получил Word и PDF. Вопросов по
файлу задавать нельзя, и это не упущение: текст файла нигде не сохраняется —
он живёт в памяти ровно на время одного обращения (§3.5). Ради второго
вопроса его пришлось бы положить в базу, а содержимому сообщений там не
место.

Действие здесь — запись в реестре config/documents.py, а не отдельный
обработчик. Новое действие не требует ни строчки в этом файле: оно приходит
в меню, в разбор нажатия и в обработку файла само.

Списание — по общему правилу: проверили остаток → сделали работу → доставили
→ списали. За упавший разбор человек не платит.
"""

from __future__ import annotations

from datetime import datetime

from app.core import texts
from app.core.actions import Action
from app.core.documents import (
    DocumentFormat,
    DocumentProblem,
    check_document,
)
from app.core.generations import GenerationKind
from app.core.limits import LimitKind
from app.core.models import ChatTurn, Document, Role
from app.core.pending import await_document
from app.core.scenarios import keyboards, paywall, spending, telemetry
from app.core.scenarios.deps import Deps, Session
from app.core.scenarios.presentations import MATERIAL_LIMIT, normalise_topic
from app.ports.ai import ContentRefusedError
from app.ports.documents import DocumentEmptyError, DocumentUnreadableError
from app.ports.handoff import Carried
from config import documents as registry
from config.documents import DocumentAction

#: Что показать по каждой причине отказа в файле.
_REJECTION_TEXTS: dict[DocumentProblem, str] = {
    DocumentProblem.TOO_BIG: texts.DOCUMENT_TOO_BIG,
    DocumentProblem.UNSUPPORTED_FORMAT: texts.DOCUMENT_UNSUPPORTED,
}

#: Короче этого тема не принимается. Одно слово вместо темы даёт сочинение
#: ни о чём, а заплатит за него человек полным разбором.
_MIN_TOPIC_LENGTH = 3

#: Действие, которым делается доклад по теме — им же и доклад по презентации.
_TOPIC_REPORT = "topic_report"

#: В каких форматах отдаём результат. Оба сразу и всегда: выбор формата ничего
#: не стоит по деньгам (модель вызывается один раз), а лишний экран стоит
#: человеку нажатия.
_OUTPUT_FORMATS: tuple[DocumentFormat, ...] = (
    DocumentFormat.DOCX,
    DocumentFormat.PDF,
)


def _choices() -> tuple[tuple[str, str], ...]:
    """Пары «подпись, идентификатор» в порядке реестра."""
    return tuple(
        (action.button, action.id) for action in registry.DOCUMENT_ACTIONS.values()
    )


def _buttons() -> tuple[str, ...]:
    """Подписи действий — для экранов, которые перечисляют их текстом."""
    return tuple(action.button for action in registry.DOCUMENT_ACTIONS.values())


async def show_menu(deps: Deps, session: Session) -> None:
    """Показывает, что бот умеет сделать с файлом."""
    await deps.storage.set_pending(session.user.id, None)
    await deps.messenger.send_text(
        session.chat,
        texts.documents_menu(_buttons()).text,
        keyboard=keyboards.documents_menu(_choices()),
    )


async def choose(deps: Deps, session: Session, action: DocumentAction) -> None:
    """Человек выбрал действие — просим файл.

    Остаток проверяется здесь, а не после присланного файла: иначе человек
    выбрал бы действие, дождался загрузки файла и только тогда узнал, что
    разборов у него не осталось.
    """
    allowance = await spending.current_allowance(deps, session, LimitKind.DOCUMENTS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.DOCUMENTS)
        return

    await deps.storage.set_pending(session.user.id, await_document(action.id))
    # Просьба берётся из реестра, а не строится по флагу: «кинь файл» и
    # «напиши тему» — разные фразы, и записаны они там же, где действие.
    screen = texts.document_ask_file(action.invitation)
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.preset_cancel()
    )


async def apply(
    deps: Deps, session: Session, action: DocumentAction, document: Document
) -> None:
    """Разбирает файл и отдаёт готовые Word и PDF."""
    check = check_document(document, max_bytes=deps.settings.max_document_bytes)
    if check.problem is not None:
        # Ожидание не трогаем: человек просто пришлёт другой файл вместо
        # этого, и заставлять его выбирать действие заново было бы глупо.
        await _reject(deps, session, _REJECTION_TEXTS[check.problem])
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.DOCUMENTS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.DOCUMENTS)
        return

    assert check.document_format is not None
    try:
        source = deps.document_reader.read(
            document, check.document_format, limit=deps.settings.document_text_limit
        )
    except DocumentUnreadableError:
        await _reject(deps, session, texts.DOCUMENT_UNREADABLE)
        return
    except DocumentEmptyError:
        await _reject(deps, session, texts.DOCUMENT_EMPTY)
        return

    await _produce(deps, session, action, source)


async def apply_topic(
    deps: Deps, session: Session, action: DocumentAction, topic: str
) -> None:
    """Делает документ по теме, которую человек написал словами.

    Файла здесь нет, и проверять нечего: тема — обычный текст сообщения.
    Слишком короткая отсекается до провайдера — «доклад» одним словом даст
    сочинение ни о чём, за которое человек заплатит разбором.
    """
    cleaned = topic.strip()
    if len(cleaned) < _MIN_TOPIC_LENGTH:
        await _reject(deps, session, texts.DOCUMENT_TOPIC_TOO_SHORT)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.DOCUMENTS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.DOCUMENTS)
        return

    await _produce(deps, session, action, cleaned, title=cleaned)


async def report_from_presentation(deps: Deps, session: Session, token: str) -> None:
    """«Сделать доклад по презентации»: доклад на её тему в одно нажатие.

    Остаток проверяется до жетона: при пустом — пейволл, а кнопка не
    сгорает и сработает, когда доклады появятся. Жетон забирается одной
    операцией: второе нажатие и повторная доставка второго доклада не
    сделают. Не вышло — жетон возвращается, и кнопку можно нажать снова.
    """
    allowance = await spending.current_allowance(deps, session, LimitKind.DOCUMENTS)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.DOCUMENTS)
        return

    handoff = deps.handoff
    carried = handoff.take(token) if handoff is not None else None
    if carried is None:
        used = handoff is not None and handoff.was_taken(token)
        screen = (
            texts.link_already_used(texts.MENU_DOCUMENTS)
            if used
            else texts.link_expired_report()
        )
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.link_exit(texts.MENU_DOCUMENTS, Action.MENU_DOCUMENTS),
        )
        return

    assert handoff is not None
    action = registry.DOCUMENT_ACTIONS[_TOPIC_REPORT]
    delivered = False
    try:
        delivered = await _produce(
            deps, session, action, carried.topic, title=carried.topic
        )
    finally:
        if not delivered:
            handoff.give_back(token, carried)


async def _produce(
    deps: Deps,
    session: Session,
    action: DocumentAction,
    source: str,
    *,
    title: str | None = None,
) -> bool:
    """Общая часть: спросить провайдера, собрать файлы, отдать, списать.

    Одна на оба входа намеренно. Разница между «по файлу» и «по теме» — это
    откуда взялся исходный текст, и только. Дальше и порядок действий, и
    правило списания обязаны совпадать, иначе один из двух путей однажды
    начнёт брать деньги за упавшую работу.
    """
    # Ожидание снимаем до обращения к провайдеру: оно может упасть, а
    # следующий присланный файл не должен приклеиться к прошлому выбору.
    await deps.storage.set_pending(session.user.id, None)

    waiting = await deps.messenger.send_text(
        session.chat, texts.document_working().text, show_menu=False
    )

    started = deps.now()
    try:
        answer = await deps.llm.complete(
            (ChatTurn(Role.USER, f"{action.instruction}\n\n{source}"),),
            model=session.model(deps.settings),
            max_tokens=deps.settings.document_max_tokens,
        )
    except ContentRefusedError as refusal:
        await _record(deps, session, action, started=started, error=refusal)
        deps.logger.info(
            "document_refused",
            user_id=int(session.user.id),
            document_action=action.id,
            reason=str(refusal),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.document_rejected(texts.DOCUMENT_EMPTY, _buttons()).text,
            keyboard=keyboards.documents_menu(_choices()),
        )
        return False
    except Exception as error:
        await _record(deps, session, action, started=started, error=error)
        deps.logger.warning(
            "document_failed",
            user_id=int(session.user.id),
            document_action=action.id,
            error=repr(error),
        )
        await deps.messenger.edit_text(
            waiting,
            texts.document_error().text,
            keyboard=keyboards.documents_menu(_choices()),
        )
        return False

    await _record(deps, session, action, started=started)

    built = [
        deps.document_writer.build(
            title or action.title, answer.text, document_format=document_format
        )
        for document_format in _OUTPUT_FORMATS
    ]
    for ready in built:
        await deps.messenger.send_document(session.chat, ready)

    await deps.messenger.edit_text(
        waiting,
        # Про обрыв говорим прямо. Упёршись в потолок длины, модель бросает
        # фразу на полуслове, и человек, не зная об этом, отдаст обрубок
        # преподавателю как готовую работу.
        texts.document_ready(_buttons(), truncated=answer.truncated).text,
        keyboard=keyboards.document_result(
            _choices(),
            presentation_token=_presentation_token(
                deps, title=title or "", fallback=action.title, text=answer.text
            ),
        ),
    )
    await spending.charge(deps, session, LimitKind.DOCUMENTS)
    return True


def _presentation_token(
    deps: Deps, *, title: str, fallback: str, text: str
) -> str | None:
    """Жетон для «Сделать презентацию по докладу»; None — кнопки не будет.

    Кнопка есть только при включённых презентациях. Под жетоном — тема и
    сам текст доклада: колода собирается по нему как по материалу. В базу
    текст не кладётся — только в память на несколько часов (порт Handoff).

    Тема — та, что человек написал; у доклада по файлу её нет, и темой
    становится первая строка доклада, его заголовок. Не годится и она —
    название действия: «Доклад», «Реферат».
    """
    if not deps.presentations_on or deps.handoff is None:
        return None
    topic = normalise_topic(title) or _heading(text) or fallback
    return deps.handoff.put(Carried(topic=topic, material=text[:MATERIAL_LIMIT]))


def _heading(text: str) -> str | None:
    """Первая непустая строка доклада без разметки — если годится в тему."""
    for line in text.splitlines():
        cleaned = line.strip().strip("#*_ ").strip()
        if cleaned:
            return normalise_topic(cleaned)
    return None


async def _reject(deps: Deps, session: Session, reason: str) -> None:
    """Говорит, почему файл не подошёл, не теряя выбранного действия."""
    screen = texts.document_rejected(reason, _buttons())
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.documents_menu(_choices())
    )


async def _record(
    deps: Deps,
    session: Session,
    action: DocumentAction,
    *,
    started: datetime,
    error: BaseException | None = None,
) -> None:
    """Учёт одного обращения к провайдеру."""
    model = session.model(deps.settings)
    if error is None:
        await telemetry.record_success(
            deps, session, GenerationKind.DOCUMENT, started=started, model=model
        )
        return
    await telemetry.record_failure(
        deps,
        session,
        GenerationKind.DOCUMENT,
        started=started,
        model=model,
        error=error,
    )
