"""Тексты людей не лежат в данных для «Повторить» (сессия 9, шаг 0).

Кнопкам «Повторить», «Ещё вариант» и «Поделиться» нужно помнить, что
повторить: сообщение чата, описание картинки, ответ чата для «Нарисуй к
этому». CLAUDE.md запрещает класть это в базу, поэтому текст живёт в памяти
процесса под жетоном, как черновик презентации, а в базе — только жетон.

Проверяется по обоим хранилищам: в памяти — что сценарий не отдаёт текст в
порт хранилища, в PostgreSQL — что его нет в самой строке человека. Память
разговора (``dialogs``) здесь не проверяется: она хранит переписку по
решению спецификации (§4.3), и это отдельный вопрос.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios import spending
from app.core.scenarios.deps import Deps, Session
from app.infra.handoff import MemoryHandoff
from tests.fakes import FakeImages, FakeLLM, FakeMessenger

TEST_DSN = os.environ.get("TEST_DATABASE_URL")

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
QUESTION = "Секретный вопрос про мой диагноз"
DESCRIPTION = "Секретное описание кота в шляпе"
ANSWER = "Секретный ответ модели про фотосинтез и хлорофилл"
SECRETS = ("Секретн",)


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest,
) -> AsyncIterator[InMemoryStorage | PostgresStorage]:
    if request.param == "memory":
        yield InMemoryStorage()
        return
    if TEST_DSN is None:
        pytest.skip("TEST_DATABASE_URL не задан")
    engine = create_engine(TEST_DSN)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE subscriptions, payments, referrals, dialogs, usage, "
                "monthly_usage, generations, users RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield PostgresStorage(engine)
    finally:
        await engine.dispose()


async def stored(storage: InMemoryStorage | PostgresStorage) -> str:
    """Всё, что лежит о человеке в хранилище, кроме памяти разговора."""
    if isinstance(storage, InMemoryStorage):
        return repr(storage._users) + repr(storage.generations)
    engine: AsyncEngine = storage._engine
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT row_to_json(u)::text FROM users u "
                "UNION ALL SELECT row_to_json(g)::text FROM generations g"
            )
        )
        return "\n".join(row[0] for row in rows.all())


def assert_clean(dump: str) -> None:
    for secret in SECRETS:
        assert secret not in dump


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


def button(messenger: FakeMessenger, label: str) -> str:
    """Действие кнопки с подписью ``label`` под последним сообщением с ней."""
    keyboards = [t.keyboard for t in messenger.texts if t.keyboard is not None]
    keyboards += [e.keyboard for e in messenger.text_edits if e.keyboard is not None]
    keyboards += [p.keyboard for p in messenger.photo_edits if p.keyboard]
    for keyboard in reversed(keyboards):
        for row in keyboard.rows:
            for found in row:
                if found.text == label and found.action is not None:
                    return found.action
    raise AssertionError(f"нет кнопки «{label}»")


async def left(deps: Deps, session: Session, kind: LimitKind) -> int:
    return (await spending.current_allowance(deps, session, kind)).total_left


async def test_a_failed_chat_message_and_its_retries_leave_no_text(
    deps: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    llm: FakeLLM,
    messenger: FakeMessenger,
) -> None:
    llm.error = RuntimeError("провайдер лёг")

    await handle(deps, incoming(text=QUESTION))
    assert messenger.last_text.text == texts.CHAT_ERROR
    assert_clean(await stored(storage))

    # «Повторить» снова падает — текста в базе по-прежнему нет.
    await handle(deps, incoming(action=Action.CHAT_RETRY))
    assert_clean(await stored(storage))

    llm.error = None
    llm.answer = ANSWER
    await handle(deps, incoming(action=Action.CHAT_RETRY))

    # Повтор спросил модель ровно о том же сообщении.
    assert all(turns[-1].content == QUESTION for turns, _ in llm.calls)
    assert len(llm.calls) == 3
    assert messenger.last_text.text == ANSWER
    assert_clean(await stored(storage))


async def test_a_picture_its_retry_and_another_variant_leave_no_text(
    deps: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    images_: FakeImages,
    messenger: FakeMessenger,
) -> None:
    images_.error = RuntimeError("провайдер лёг")

    await handle(deps, incoming(action=Action.MENU_IMAGES))
    await handle(deps, incoming(text=DESCRIPTION))
    assert_clean(await stored(storage))

    await handle(deps, incoming(action=Action.IMAGE_RETRY))
    assert_clean(await stored(storage))

    images_.error = None
    await handle(deps, incoming(action=Action.IMAGE_RETRY))
    assert_clean(await stored(storage))

    await handle(deps, incoming(action=Action.IMAGE_AGAIN))
    assert_clean(await stored(storage))

    assert [prompt for prompt, _ in images_.generated] == [DESCRIPTION] * 4
    assert len(messenger.photo_edits) == 2


async def test_draw_this_and_its_retry_leave_no_answer_text(
    deps: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    llm: FakeLLM,
    images_: FakeImages,
    messenger: FakeMessenger,
) -> None:
    llm.answer = ANSWER
    await handle(deps, incoming(text="Расскажи про листья"))
    draw = button(messenger, texts.BUTTON_DRAW_THIS)
    images_.error = RuntimeError("провайдер лёг")

    await handle(deps, incoming(action=draw))
    assert_clean(await stored(storage))

    images_.error = None
    await handle(deps, incoming(action=Action.IMAGE_RETRY))
    assert_clean(await stored(storage))

    await handle(deps, incoming(action=Action.IMAGE_AGAIN))
    assert_clean(await stored(storage))

    drawn = [prompt for prompt, _ in images_.generated]
    assert len(drawn) == 3
    assert all(ANSWER in prompt for prompt in drawn)


async def test_after_a_restart_retry_answers_honestly_and_spends_nothing(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    images_: FakeImages,
    messenger: FakeMessenger,
) -> None:
    """Текст жил в памяти процесса: после перезапуска повторять нечего."""
    llm.error = RuntimeError("провайдер лёг")
    images_.error = RuntimeError("провайдер лёг")
    await handle(deps, incoming(text=QUESTION))
    await handle(deps, incoming(action=Action.MENU_IMAGES))
    await handle(deps, incoming(text=DESCRIPTION))
    llm.error = images_.error = None
    messages = await left(deps, session, LimitKind.MESSAGES)
    pictures = await left(deps, session, LimitKind.IMAGES)

    restarted = replace(deps, prompts=MemoryHandoff())
    for action in (Action.CHAT_RETRY, Action.IMAGE_RETRY, Action.IMAGE_AGAIN):
        await handle(restarted, incoming(action=action))
        assert messenger.last_text.text == texts.NOTHING_TO_REPEAT

    assert len(llm.calls) == 1
    assert len(images_.generated) == 1
    assert await left(deps, session, LimitKind.MESSAGES) == messages
    assert await left(deps, session, LimitKind.IMAGES) == pictures
