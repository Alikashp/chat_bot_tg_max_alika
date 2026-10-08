"""Отметка «человек остановил бота» (сессия 8, О1, О2).

Ставится по событию мессенджера (блокировка, остановка, удаление бота) и по
отказу мессенджера доставить сообщение этому человеку. Снимается любым
действием человека: сообщением или нажатием кнопки. Поведение бота от
отметки не меняется — она для будущей рассылки и статистики.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace

from app.adapters.storage.memory import InMemoryStorage
from app.core.actions import Action
from app.core.models import Chat, Document, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps, session_for
from app.core.scenarios.reach import WatchedMessenger
from app.ports.messenger import RecipientGoneError
from tests.fakes import FakeLLM, FakeLogger, FakeMessenger, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1", person="1")


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


def watched(deps: Deps, messenger: FakeMessenger) -> Deps:
    return replace(
        deps,
        messenger=WatchedMessenger(
            messenger, storage=deps.storage, logger=deps.logger, now=deps.now
        ),
    )


async def stopped_at(storage: InMemoryStorage, user: User) -> object:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.stopped_at


# --- По событию мессенджера --------------------------------------------


async def test_the_stop_event_sets_the_mark_and_says_nothing(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    await handle(deps, incoming(stopped=True))

    assert await stopped_at(storage, user) == clock.now
    assert messenger.texts == []


async def test_a_stop_event_from_a_stranger_creates_nobody(
    deps: Deps, storage: InMemoryStorage
) -> None:
    await handle(
        deps,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="404"),
            external_user_id="404",
            stopped=True,
        ),
    )

    assert await storage.get_user(MessengerKind.TELEGRAM, "404") is None


async def test_a_message_takes_the_mark_off(
    deps: Deps, user: User, storage: InMemoryStorage
) -> None:
    await handle(deps, incoming(stopped=True))

    await handle(deps, incoming(text="я вернулся"))

    assert await stopped_at(storage, user) is None


async def test_a_button_press_takes_the_mark_off(
    deps: Deps, user: User, storage: InMemoryStorage
) -> None:
    await handle(deps, incoming(stopped=True))

    await handle(deps, incoming(action=Action.MENU_PROFILE))

    assert await stopped_at(storage, user) is None


async def test_the_mark_changes_nothing_for_the_person(
    deps: Deps, user: User, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    """Пока отметка — только знание. Вернулся — бот отвечает как прежде."""
    await handle(deps, incoming(stopped=True))
    llm.answer = "Ответ."

    await handle(deps, incoming(text="привет"))

    assert messenger.last_text.text == "Ответ."


# --- По отказу при отправке ---------------------------------------------


async def test_a_refused_answer_sets_the_mark(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Человек заблокировал бота, пока тот думал: ответ уже не доставить."""
    live = watched(deps, messenger)
    messenger.fail_send = RecipientGoneError()

    await handle(live, incoming(text="привет"))

    assert await stopped_at(storage, user) == clock.now


async def test_a_refused_message_we_start_sets_the_mark(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Разговор начинаем мы (напоминание, оплата): адресат — сам человек."""
    live = watched(deps, messenger)
    messenger.fail_send = RecipientGoneError()

    with contextlib.suppress(RecipientGoneError):
        await live.messenger.send_text(session_for(live, user).chat, "напоминание")

    assert await stopped_at(storage, user) == clock.now


async def test_the_refusal_still_reaches_the_caller(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Отметка — не повод делать вид, что сообщение дошло."""
    live = watched(deps, messenger)
    messenger.fail_send = RecipientGoneError()

    raised = False
    try:
        await live.messenger.send_text(CHAT, "текст")
    except RecipientGoneError:
        raised = True

    assert raised


async def test_another_failure_sets_no_mark(
    deps: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Сеть моргнула — это не «остановил бота»."""
    live = watched(deps, messenger)
    messenger.fail_send = RuntimeError("таймаут")

    with contextlib.suppress(RuntimeError):
        await live.messenger.send_text(CHAT, "текст")

    assert await stopped_at(storage, user) is None


async def test_every_kind_of_delivery_is_watched(
    deps: Deps, user: User, storage: InMemoryStorage
) -> None:
    """Не только текст: картинка, файл, правка — отказ любого ставит отметку."""

    class Refusing(FakeMessenger):
        async def send_document(self, chat: Chat, document: Document) -> None:
            raise RecipientGoneError()

    inner = Refusing()
    live = watched(deps, inner)
    with contextlib.suppress(RecipientGoneError):
        await live.messenger.send_document(
            CHAT, Document(data=b"x", filename="a.txt", mime_type="text/plain")
        )

    assert await stopped_at(storage, user) is not None


# --- О2: в логах — только идентификатор ---------------------------------


async def test_the_log_names_the_user_and_nothing_else(
    deps: Deps, user: User, messenger: FakeMessenger, logger: FakeLogger
) -> None:
    live = watched(deps, messenger)
    messenger.fail_send = RecipientGoneError()
    await handle(live, incoming(text="секретный вопрос"))
    await handle(live, incoming(stopped=True))

    events = [(e.event, e.fields) for e in logger.events]
    marked = [fields for event, fields in events if event == "user_stopped_bot"]
    assert marked and all(set(fields) <= {"user_id", "source"} for fields in marked)
    assert "секретный" not in str(events)


async def test_the_mark_goes_to_the_owner_of_the_chat(
    deps: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """В MAX номер переписки человека не называет — называет поле person."""
    live = watched(deps, messenger)
    messenger.fail_send = RecipientGoneError()
    dialog = Chat(messenger=MessengerKind.TELEGRAM, chat_id="999", person="1")

    with contextlib.suppress(RecipientGoneError):
        await live.messenger.send_text(dialog, "текст")

    assert await stopped_at(storage, user) is not None
