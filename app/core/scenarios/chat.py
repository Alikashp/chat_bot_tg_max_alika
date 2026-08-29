"""Чат (§2.2).

Написал сообщение → получил ответ. Всё.

Модель не выбирается пользователем и нигде не упоминается, контекст помнится
всегда, переключателя нет. Кнопка «🔄 Новый диалог» появляется под ответом
начиная с десятого сообщения — раньше она только мешает.
"""

from __future__ import annotations

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import ChatTurn, Role
from app.core.retry_context import RetryContext, RetryKind
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, Session


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
        answer,
        keyboard=keyboards.new_dialog() if offer_new_dialog else None,
    )

    # Сюда попадаем только после доставки — теперь можно списывать.
    await deps.storage.set_retry_context(session.user.id, None)
    await deps.storage.save_dialog(
        session.user.id,
        asked.appended(
            ChatTurn(Role.ASSISTANT, answer),
            max_turns=deps.settings.dialog_max_turns,
        ),
    )
    await spending.charge(deps, session, LimitKind.MESSAGES)


async def start_new_dialog(deps: Deps, session: Session) -> None:
    """«🔄 Новый диалог»: забываем контекст и говорим об этом."""
    await deps.storage.reset_dialog(session.user.id)
    screen = texts.new_dialog_started()
    await deps.messenger.send_text(session.chat, screen.text, show_menu=True)
