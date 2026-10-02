"""Меню обновляется без /start (Д6, §4.2).

Постоянная клавиатура Telegram меняется только сообщением, которое её несёт.
После выкладки с новым меню человек, не нажимавший /start, видел бы старое
сколько угодно. Поэтому у человека в базе — версия меню, которую он видел,
и с первым ответом бота после выкладки меню обновляется. Один раз на версию.
"""

from __future__ import annotations

from dataclasses import replace

from app.adapters.storage.memory import InMemoryStorage
from app.core.actions import Action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps
from tests.fakes import FakeMessenger, FakePresentations

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


async def test_the_first_answer_after_a_release_refreshes_the_menu(
    deps: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """Д6: человек со старым меню получает новое с первым ответом — и всё."""
    assert user.menu_version is None

    await handle(deps, incoming(action=Action.MENU_PROFILE))
    await handle(deps, incoming(text="привет"))
    await handle(deps, incoming(action=Action.MENU_IMAGES))

    assert messenger.menu_refreshes == [CHAT]
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    assert fresh.menu_version == keyboards.menu_version(presentations=False)


async def test_the_refresh_comes_after_the_answer(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Меню обновляется вместе с ответом, а не вместо него."""
    await handle(deps, incoming(action=Action.MENU_PROFILE))

    assert messenger.texts, "ответ должен прийти"
    assert messenger.menu_refreshes == [CHAT]


async def test_someone_with_the_current_menu_gets_no_extra_message(
    deps: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """Без смены меню — ни одного лишнего обновления."""
    await storage.set_menu_version(user.id, keyboards.menu_version(presentations=False))

    await handle(deps, incoming(action=Action.MENU_PROFILE))

    assert messenger.menu_refreshes == []


async def test_start_counts_as_seeing_the_menu(
    deps: Deps, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """/start сам приносит меню, и второго обновления следом не нужно."""
    await handle(deps, incoming(start_payload=""))
    await handle(deps, incoming(action=Action.MENU_PROFILE))

    assert messenger.menu_refreshes == []


async def test_switching_presentations_on_is_a_new_menu(
    deps: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """Ключ API появился — в меню новая кнопка, и меню обновляется снова."""
    await handle(deps, incoming(action=Action.MENU_PROFILE))
    enabled = replace(deps, presentations=FakePresentations())

    await handle(enabled, incoming(action=Action.MENU_PROFILE))
    await handle(enabled, incoming(action=Action.MENU_PROFILE))

    assert messenger.menu_refreshes == [CHAT, CHAT]
    assert keyboards.menu_version(presentations=True) != keyboards.menu_version(
        presentations=False
    )
