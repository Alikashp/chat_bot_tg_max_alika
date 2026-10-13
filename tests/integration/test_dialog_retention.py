"""История переписки уходит из базы (сессия 10, шаг 7, С7).

Память разговора — обещание продукта (spec §4.3) и единственное
исключение из запрета хранить тексты в базе. Но и у него есть граница:
«Новый диалог» удаляет историю человека, а история без активности 30 дней
удаляется сама. Проверяется по обоим хранилищам: в PostgreSQL — что строки
в ``dialogs`` правда нет.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.storage.schema import metadata
from app.core import retention, support
from app.core.actions import Action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from tests.fakes import FrozenClock

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
SECRET = "Секрет из переписки"


@pytest.fixture(params=["memory", "postgres"])
async def storage(
    request: pytest.FixtureRequest, clock: FrozenClock
) -> AsyncIterator[InMemoryStorage | PostgresStorage]:
    if request.param == "memory":
        yield InMemoryStorage(clock=clock)
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
        yield PostgresStorage(engine, clock=clock)
    finally:
        await engine.dispose()


async def stored_dialogs(storage: InMemoryStorage | PostgresStorage) -> str:
    """Всё, что лежит в памяти разговоров."""
    if isinstance(storage, InMemoryStorage):
        return repr(storage._dialogs)
    async with storage._engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT user_id, turns::text FROM dialogs")
        )
        return repr(rows.all())


def said(person: str, words: str) -> IncomingMessage:
    return IncomingMessage(
        chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id=person),
        external_user_id=person,
        text=words,
    )


async def test_a_new_dialog_deletes_the_history(
    deps: Deps, user: User, storage: InMemoryStorage | PostgresStorage
) -> None:
    await handle(deps, said("1", SECRET))
    assert SECRET in await stored_dialogs(storage)

    await handle(
        deps,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="1"),
            external_user_id="1",
            action=Action.CHAT_NEW_DIALOG,
        ),
    )

    assert SECRET not in await stored_dialogs(storage)
    assert str(int(user.id)) not in await stored_dialogs(storage)


async def test_a_history_idle_for_30_days_is_deleted(
    deps: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    clock: FrozenClock,
) -> None:
    await handle(deps, said("1", SECRET))
    clock.advance(days=29)
    other = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="2",
        referral_code="code2",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    await handle(deps, said("2", "Свежий вопрос"))

    await retention.forget_idle_dialogs(deps)
    assert SECRET in await stored_dialogs(storage)

    clock.advance(days=1, seconds=1)
    await retention.forget_idle_dialogs(deps)

    left = await stored_dialogs(storage)
    assert SECRET not in left
    assert "Свежий вопрос" in left
    assert (await storage.get_dialog(other.id)).turns


async def test_talking_again_keeps_the_history(
    deps: Deps,
    user: User,
    storage: InMemoryStorage | PostgresStorage,
    clock: FrozenClock,
) -> None:
    """30 дней считаются от последней реплики, а не от первой."""
    await handle(deps, said("1", SECRET))
    clock.advance(days=20)
    await handle(deps, said("1", "Ещё вопрос"))
    clock.advance(days=20)

    await retention.forget_idle_dialogs(deps)

    assert SECRET in await stored_dialogs(storage)
