"""Отметка последней активности человека (сессия 12, шаг 2).

``users.last_active_at`` — от неё считается цепочка реактивации. Обновляется
на любое входящее обновление от человека в обоих мессенджерах: сообщение,
кнопку, команду, оплату. Не чаще раза в час — чтобы не писать в базу на
каждое сообщение. Событие «остановил бота» активностью не считается.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support
from app.core.actions import Action
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.router import handle
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps
from tests.fakes import FakeStars, FrozenClock


async def person(storage: InMemoryStorage, messenger: MessengerKind) -> User:
    return await storage.create_user(
        messenger=messenger,
        external_id="7",
        referral_code=f"code-{messenger.value}",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )


def chat(messenger: MessengerKind) -> Chat:
    return Chat(messenger=messenger, chat_id="7", person="7")


async def active_at(storage: InMemoryStorage, user: User) -> object:
    found = await storage.get_user_by_id(user.id)
    assert found is not None
    return found.last_active_at


async def test_a_new_person_is_active_from_registration(
    storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await person(storage, MessengerKind.TELEGRAM)

    assert user.last_active_at == clock.now


@pytest.mark.parametrize("kind", [MessengerKind.TELEGRAM, MessengerKind.MAX])
@pytest.mark.parametrize(
    "fields",
    [
        {"text": "привет"},
        {"action": Action.MENU_PROFILE},
        {"start_payload": ""},
    ],
    ids=["message", "button", "command"],
)
async def test_any_update_from_the_person_marks_activity(
    deps: Deps,
    storage: InMemoryStorage,
    clock: FrozenClock,
    kind: MessengerKind,
    fields: dict[str, object],
) -> None:
    user = await person(storage, kind)
    clock.advance(days=4)

    await handle(
        deps,
        IncomingMessage(chat=chat(kind), external_user_id="7", **fields),  # type: ignore[arg-type]
    )

    assert await active_at(storage, user) == clock.now


async def test_a_payment_marks_activity(
    deps: Deps, storage: InMemoryStorage, stars: FakeStars, clock: FrozenClock
) -> None:
    user = await person(storage, MessengerKind.TELEGRAM)
    from app.core.scenarios.deps import session_for

    await payments.start_stars(deps, session_for(deps, user), TariffId.PRO)
    clock.advance(days=2)

    await handle(
        deps,
        IncomingMessage(
            chat=chat(MessengerKind.TELEGRAM),
            external_user_id="7",
            paid_order_id=stars.invoices[0].order_id,
            paid_charge_id="charge-1",
        ),
    )

    assert await active_at(storage, user) == clock.now


async def test_activity_is_written_at_most_once_an_hour(
    deps: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await person(storage, MessengerKind.TELEGRAM)
    clock.advance(days=1)
    message = IncomingMessage(
        chat=chat(MessengerKind.TELEGRAM), external_user_id="7", text="привет"
    )
    writes: list[object] = []
    touch = storage.touch_active

    async def counted(*args: object, **kwargs: object) -> None:
        writes.append(args)
        await touch(*args, **kwargs)  # type: ignore[arg-type]

    storage.touch_active = counted  # type: ignore[method-assign]

    await handle(deps, message)
    first = clock.now
    clock.advance(minutes=59)
    await handle(deps, message)
    assert len(writes) == 1
    assert await active_at(storage, user) == first

    clock.advance(minutes=2)
    await handle(deps, message)

    assert len(writes) == 2
    assert await active_at(storage, user) == clock.now


async def test_stopping_the_bot_is_not_activity(
    deps: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await person(storage, MessengerKind.TELEGRAM)
    registered = clock.now
    clock.advance(days=3)

    await handle(
        deps,
        IncomingMessage(
            chat=chat(MessengerKind.TELEGRAM), external_user_id="7", stopped=True
        ),
    )

    assert await active_at(storage, user) == registered


async def test_activity_never_moves_back(
    storage: InMemoryStorage, clock: FrozenClock
) -> None:
    """Запись с опоздавшим временем не отодвигает отметку назад."""
    user = await person(storage, MessengerKind.TELEGRAM)
    later = clock.now + timedelta(days=1)

    await storage.touch_active(user.id, later)
    await storage.touch_active(user.id, clock.now)

    assert await active_at(storage, user) == later
