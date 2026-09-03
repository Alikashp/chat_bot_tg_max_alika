"""Имя пользователя в мессенджере: единственное правило — оно должно быть свежим.

Записанное при регистрации и не обновлённое имя через месяц указывает не на
того человека. Это хуже, чем не записать вовсе: по нему поддержка пойдёт
искать и найдёт постороннего — с чужой перепиской и чужими платежами.
"""

from __future__ import annotations

from app.adapters.storage.memory import InMemoryStorage
from app.core.models import NO_USERNAME, MessengerKind, User
from app.core.router import handle
from app.core.scenarios import identity
from app.core.scenarios.deps import Deps
from tests.fakes import FakeLogger
from tests.unit.test_router import incoming


async def test_a_changed_name_is_written_down(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    refreshed = await identity.remember_username(deps, user, "newname")

    saved = await storage.get_user_by_id(user.id)
    assert saved is not None
    assert saved.username == "newname"
    assert refreshed.username == "newname"


async def test_the_same_name_costs_no_write(
    deps: Deps, storage: InMemoryStorage, user: User, logger: FakeLogger
) -> None:
    """В обычной жизни это сравнение двух строк и ни одного запроса."""
    await identity.remember_username(deps, user, "samename")
    logger.events.clear()

    stored = await storage.get_user_by_id(user.id)
    assert stored is not None
    await identity.remember_username(deps, stored, "samename")

    assert [e.event for e in logger.events] == []


async def test_an_empty_field_means_the_name_is_gone(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Пустое поле — это ответ мессенджера, а не его молчание.

    И Telegram, и MAX присылают пользователя целиком при каждом обращении.
    Поэтому пустота означает «человек снял себе имя», и оставить прежнее
    значило бы держать в базе имя, которого у него больше нет.
    """
    await identity.remember_username(deps, user, "hadaname")
    stored = await storage.get_user_by_id(user.id)
    assert stored is not None

    await identity.remember_username(deps, stored, None)

    saved = await storage.get_user_by_id(user.id)
    assert saved is not None
    assert saved.username == NO_USERNAME


async def test_removing_the_name_is_recorded(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Человек снял имя — «NONE» честнее, чем прежнее, которого уже нет."""
    await identity.remember_username(deps, user, "hadaname")
    stored = await storage.get_user_by_id(user.id)
    assert stored is not None

    await identity.remember_username(deps, stored, "")

    saved = await storage.get_user_by_id(user.id)
    assert saved is not None
    assert saved.username == NO_USERNAME


async def test_the_at_sign_is_not_part_of_the_name(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """В базе ищут по имени, а не по тому, как его пишут в переписке."""
    await identity.remember_username(deps, user, "@durov")

    saved = await storage.get_user_by_id(user.id)
    assert saved is not None
    assert saved.username == "durov"


# --- Через маршрутизатор, как в бою --------------------------------------


async def test_any_message_refreshes_the_name(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    await handle(deps, incoming(text="привет", username="afterwards"))

    saved = await storage.get_user_by_id(user.id)
    assert saved is not None
    assert saved.username == "afterwards"


async def test_a_new_person_gets_their_name_at_once(
    deps: Deps, storage: InMemoryStorage
) -> None:
    """Первое же обращение — уже с именем, отдельной правки не требуется."""
    await handle(deps, incoming(text="/start", start_payload="", username="fresh"))

    created = await storage.get_user(MessengerKind.TELEGRAM, "1")
    assert created is not None
    assert created.username == "fresh"


async def test_a_new_person_without_a_name_is_marked_none(
    deps: Deps, storage: InMemoryStorage
) -> None:
    await handle(deps, incoming(text="/start", start_payload=""))

    created = await storage.get_user(MessengerKind.TELEGRAM, "1")
    assert created is not None
    assert created.username == NO_USERNAME


async def test_the_name_is_never_shown_to_the_person(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Это строчка для поддержки, а не экран бота."""
    from tests.fakes import FakeMessenger

    await handle(deps, incoming(text="привет", username="secretname"))
    await handle(deps, incoming(action="m:me", username="secretname"))

    messenger = deps.messenger
    assert isinstance(messenger, FakeMessenger)
    assert all("secretname" not in said for said in messenger.texts_said())
