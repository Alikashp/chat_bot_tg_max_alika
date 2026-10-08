"""Чат (§2.2).

Написал сообщение → получил ответ. Всё.

Модель не выбирается пользователем и нигде не упоминается, контекст помнится
всегда, переключателя нет. Кнопка «🔄 Новый диалог» появляется под ответом
начиная с десятого сообщения — раньше она только мешает.

Под каждым ответом — кнопки продолжения (сессия 8): «Объясни проще»,
«Короче», «Нарисуй к этому». Ответ они находят в памяти разговора по
отпечатку: память разговора уже хранит ответы, и второе хранилище для них
было бы лишним. Нет ответа в памяти — кнопка честно говорит, что его нет.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from app.core import texts
from app.core.actions import Action, Followup, followup_action
from app.core.generations import GenerationKind
from app.core.limits import LimitKind
from app.core.models import ChatTurn, DialogState, Role
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import images, keyboards, paywall, spending, telemetry
from app.core.scenarios.deps import Deps, Session
from config.prompt import (
    CONTINUE_PROMPT,
    DRAW_PROMPT,
    DRAW_SOURCE_MAX_CHARS,
    SHORTER_PROMPT,
    SIMPLER_PROMPT,
)

#: Сколько начальных знаков ответа идёт в отпечаток. По началу, а не по
#: всему тексту: «Продолжить» дописывает ответ в ту же реплику, и кнопки под
#: его началом должны находить реплику и после этого. Оборванный ответ
#: всегда длиннее этого порога — он упёрся в потолок длины.
MARK_SOURCE_CHARS = 256

_PROMPTS = {Followup.SIMPLER: SIMPLER_PROMPT, Followup.SHORTER: SHORTER_PROMPT}


def answer_mark(answer: str) -> str:
    """Отпечаток ответа для данных кнопки: 12 знаков вместо самого текста."""
    return hashlib.sha256(answer[:MARK_SOURCE_CHARS].encode()).hexdigest()[:12]


async def handle_message(deps: Deps, session: Session, text: str) -> None:
    """Отвечает на сообщение пользователя."""
    allowance = await spending.current_allowance(deps, session, LimitKind.MESSAGES)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.MESSAGES)
        return

    await deps.messenger.send_typing(session.chat)

    dialog = await deps.storage.get_dialog(session.user.id)
    asked = dialog.appended(
        ChatTurn(Role.USER, text), max_turns=deps.settings.dialog_max_turns
    )

    model = session.model(deps.settings)
    started = deps.now()
    try:
        answer = await deps.llm.complete(asked.turns, model=model)
    except Exception as error:
        await telemetry.record_failure(
            deps,
            session,
            GenerationKind.CHAT,
            started=started,
            model=model,
            error=error,
        )
        # Лимит не тронут: пользователь получит ошибку и сможет повторить,
        # ничего не потеряв. Это обещано ему прямо в тексте.
        deps.logger.warning(
            "llm_failed", user_id=int(session.user.id), error=repr(error)
        )
        # Запоминаем сообщение, чтобы «Повторить» повторяло именно его, а не
        # просило человека набрать всё заново.
        await deps.storage.set_retry_context(
            session.user.id,
            RetryContext(kind=RetryKind.CHAT, prompt=text).encode(),
        )
        screen = texts.chat_error()
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.retry(Action.CHAT_RETRY),
        )
        return

    await telemetry.record_success(
        deps,
        session,
        GenerationKind.CHAT,
        started=started,
        model=model,
        tokens_in=answer.tokens_in,
        tokens_out=answer.tokens_out,
    )

    offer_new_dialog = asked.user_turns >= deps.settings.new_dialog_after_messages
    await deps.messenger.send_text(
        session.chat,
        answer.text,
        keyboard=keyboards.chat_answer(
            mark=answer_mark(answer.text),
            truncated=answer.truncated,
            offer_new_dialog=offer_new_dialog,
        ),
    )

    # Сюда попадаем только после доставки — теперь можно списывать.
    await deps.storage.set_retry_context(session.user.id, None)
    await deps.storage.save_dialog(
        session.user.id,
        asked.appended(
            ChatTurn(Role.ASSISTANT, answer.text),
            max_turns=deps.settings.dialog_max_turns,
        ),
    )
    await spending.charge(deps, session, LimitKind.MESSAGES)


async def continue_answer(deps: Deps, session: Session) -> None:
    """«▶️ Продолжить»: досказывает ответ, оборванный потолком длины.

    Просьба досказать уходит провайдеру, но в историю диалога не попадает: за
    несколько нажатий переписка состояла бы наполовину из наших же служебных
    строк, и модель отвечала бы уже на них. В истории вместо этого растёт
    сам ответ — обе половины склеиваются в одну реплику, как если бы модель
    сказала это разом.

    Продолжение стоит сообщения. Это полноценный запрос к провайдеру, и
    делать его бесплатным значило бы раздавать длинные ответы в обход лимита.
    """
    dialog = await deps.storage.get_dialog(session.user.id)
    if not dialog.turns or dialog.turns[-1].role is not Role.ASSISTANT:
        # Кнопка из давнего сообщения: разговор с тех пор ушёл вперёд или
        # его начали заново. Продолжать нечего, но и молчать нельзя.
        screen = texts.nothing_to_repeat()
        await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
        return

    allowance = await spending.current_allowance(deps, session, LimitKind.MESSAGES)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.MESSAGES)
        return

    await deps.messenger.send_typing(session.chat)
    asked = dialog.appended(
        ChatTurn(Role.USER, CONTINUE_PROMPT),
        max_turns=deps.settings.dialog_max_turns,
    )

    model = session.model(deps.settings)
    started = deps.now()
    try:
        answer = await deps.llm.complete(asked.turns, model=model)
    except Exception as error:
        await telemetry.record_failure(
            deps,
            session,
            GenerationKind.CHAT,
            started=started,
            model=model,
            error=error,
        )
        deps.logger.warning(
            "llm_continue_failed", user_id=int(session.user.id), error=repr(error)
        )
        screen = texts.chat_error()
        await deps.messenger.send_text(
            session.chat, screen.text, keyboard=keyboards.retry(Action.CHAT_CONTINUE)
        )
        return

    await telemetry.record_success(
        deps,
        session,
        GenerationKind.CHAT,
        started=started,
        model=model,
        tokens_in=answer.tokens_in,
        tokens_out=answer.tokens_out,
    )

    merged = _merged(dialog, answer.text, deps.settings.dialog_max_turns)
    await deps.messenger.send_text(
        session.chat,
        answer.text,
        keyboard=keyboards.chat_answer(
            # Отпечаток всей склеенной реплики: кнопки под продолжением и под
            # началом ведут к одному и тому же ответу.
            mark=answer_mark(merged.turns[-1].content),
            truncated=answer.truncated,
            offer_new_dialog=dialog.user_turns
            >= deps.settings.new_dialog_after_messages,
        ),
    )

    await deps.storage.save_dialog(session.user.id, merged)
    await spending.charge(deps, session, LimitKind.MESSAGES)


def _merged(dialog: DialogState, continuation: str, max_turns: int) -> DialogState:
    """Дописывает продолжение к последней реплике модели, а не рядом с ней.

    Две реплики подряд от модели выглядели бы для неё самой как два разных
    ответа, и на следующем вопросе она путалась бы, какой из них считать
    своим последним словом.
    """
    head = dialog.turns[:-1]
    whole = ChatTurn(Role.ASSISTANT, f"{dialog.turns[-1].content}{continuation}")
    return replace(dialog, turns=(*head, whole)[-max_turns:] if max_turns > 0 else ())


async def follow_up(deps: Deps, session: Session, kind: Followup, mark: str) -> None:
    """Кнопка продолжения под ответом: проще, короче или картинка к нему.

    Каждое нажатие — обычный запрос: «проще» и «короче» стоят сообщения,
    картинка — картинки; списание после доставки, без остатка — пейволл.
    """
    dialog = await deps.storage.get_dialog(session.user.id)
    found = _answer_by_mark(dialog, mark)
    if found is None:
        # Разговор начат заново или ушёл дальше окна памяти. Угадывать, о
        # каком ответе речь, нельзя — говорим как есть и даём меню.
        deps.logger.info(
            "chat_followup_gone", user_id=int(session.user.id), kind=kind.value
        )
        screen = texts.chat_answer_gone()
        await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
        return

    target = dialog.turns[found]
    if kind is Followup.DRAW:
        description = DRAW_PROMPT.format(text=target.content[:DRAW_SOURCE_MAX_CHARS])
        await images.draw(deps, session, description)
        return

    await _rework(deps, session, dialog, found, kind, followup_action(kind, mark))


def _answer_by_mark(dialog: DialogState, mark: str) -> int | None:
    """Индекс ответа с этим отпечатком; из совпавших — последний."""
    for index in range(len(dialog.turns) - 1, -1, -1):
        turn = dialog.turns[index]
        if turn.role is Role.ASSISTANT and answer_mark(turn.content) == mark:
            return index
    return None


async def _rework(
    deps: Deps,
    session: Session,
    dialog: DialogState,
    found: int,
    kind: Followup,
    action: str,
) -> None:
    """«Объясни проще» и «Короче»: тот же ответ, пересказанный иначе.

    Модель видит разговор до этого ответа включительно и просьбу: под
    давним ответом пересказывается он, а не последний. В память разговора
    просьба и пересказ ложатся в конец — так, как если бы человек написал
    просьбу сам.
    """
    allowance = await spending.current_allowance(deps, session, LimitKind.MESSAGES)
    if allowance.exhausted:
        await paywall.show(deps, session, LimitKind.MESSAGES)
        return

    await deps.messenger.send_typing(session.chat)
    request = ChatTurn(Role.USER, _PROMPTS[kind])
    turns = (*dialog.turns[: found + 1], request)

    model = session.model(deps.settings)
    started = deps.now()
    try:
        answer = await deps.llm.complete(turns, model=model)
    except Exception as error:
        await telemetry.record_failure(
            deps,
            session,
            GenerationKind.CHAT,
            started=started,
            model=model,
            error=error,
        )
        deps.logger.warning(
            "llm_followup_failed",
            user_id=int(session.user.id),
            kind=kind.value,
            error=repr(error),
        )
        # «Повторить» — то же нажатие: отпечаток в нём, а текстов в базе нет.
        await deps.messenger.send_text(
            session.chat,
            texts.chat_error().text,
            keyboard=keyboards.followup_retry(action),
        )
        return

    await telemetry.record_success(
        deps,
        session,
        GenerationKind.CHAT,
        started=started,
        model=model,
        tokens_in=answer.tokens_in,
        tokens_out=answer.tokens_out,
    )

    max_turns = deps.settings.dialog_max_turns
    asked = dialog.appended(request, max_turns=max_turns)
    await deps.messenger.send_text(
        session.chat,
        answer.text,
        keyboard=keyboards.chat_answer(
            mark=answer_mark(answer.text),
            truncated=answer.truncated,
            offer_new_dialog=asked.user_turns
            >= deps.settings.new_dialog_after_messages,
        ),
    )

    await deps.storage.save_dialog(
        session.user.id,
        asked.appended(ChatTurn(Role.ASSISTANT, answer.text), max_turns=max_turns),
    )
    await spending.charge(deps, session, LimitKind.MESSAGES)


async def start_new_dialog(deps: Deps, session: Session) -> None:
    """«🔄 Новый диалог»: забываем контекст и говорим об этом."""
    await deps.storage.reset_dialog(session.user.id)
    screen = texts.new_dialog_started()
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
