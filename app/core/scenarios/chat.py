"""Чат (§2.2).

Написал сообщение → получил ответ. Всё.

Модель не выбирается пользователем и нигде не упоминается, контекст помнится
всегда, переключателя нет. Кнопка «🔄 Новый диалог» появляется под ответом
начиная с десятого сообщения — раньше она только мешает.
"""

from __future__ import annotations

from dataclasses import replace

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import ChatTurn, DialogState, Role
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session
from config.prompt import CONTINUE_PROMPT


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

    try:
        answer = await deps.llm.complete(
            asked.turns, model=session.model(deps.settings)
        )
    except Exception as error:
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

    offer_new_dialog = asked.user_turns >= deps.settings.new_dialog_after_messages
    await deps.messenger.send_text(
        session.chat,
        answer.text,
        keyboard=keyboards.chat_answer(
            truncated=answer.truncated, offer_new_dialog=offer_new_dialog
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

    try:
        answer = await deps.llm.complete(
            asked.turns, model=session.model(deps.settings)
        )
    except Exception as error:
        deps.logger.warning(
            "llm_continue_failed", user_id=int(session.user.id), error=repr(error)
        )
        screen = texts.chat_error()
        await deps.messenger.send_text(
            session.chat, screen.text, keyboard=keyboards.retry(Action.CHAT_CONTINUE)
        )
        return

    await deps.messenger.send_text(
        session.chat,
        answer.text,
        keyboard=keyboards.chat_answer(
            truncated=answer.truncated,
            offer_new_dialog=dialog.user_turns
            >= deps.settings.new_dialog_after_messages,
        ),
    )

    await deps.storage.save_dialog(
        session.user.id, _merged(dialog, answer.text, deps.settings.dialog_max_turns)
    )
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


async def start_new_dialog(deps: Deps, session: Session) -> None:
    """«🔄 Новый диалог»: забываем контекст и говорим об этом."""
    await deps.storage.reset_dialog(session.user.id)
    screen = texts.new_dialog_started()
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
