"""Кнопки после готовой презентации и перегенерация (сессия 12, шаг 1, Р1).

Под итогом после файлов — три кнопки, каждая своей строкой: «🔄
Перегенерировать», «📄 Доклад по презентации», «✅ Новая презентация».
Перегенерация — новая сборка с теми же параметрами, по черновику в памяти
(тема в базу не пишется, В6), и списывается как обычная сборка. На
бесплатном тарифе — одна бесплатная перегенерация первой бесплатной
презентации, один раз на человека.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.adapters.max import keyboards as max_keyboards
from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action, parse_report_from_action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from app.infra.handoff import MemoryHandoff
from tests.fakes import FakeMessenger, FakePresentations, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
TOPIC = "Как работает фотосинтез"
LABELS = [
    "🔄 Перегенерировать",
    "📄 Доклад по презентации",
    "✅ Новая презентация",
]


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


@pytest.fixture
def presentations() -> FakePresentations:
    return FakePresentations()


@pytest.fixture
def enabled(deps: Deps, presentations: FakePresentations) -> Deps:
    return replace(deps, presentations=presentations)


async def left(storage: InMemoryStorage, user: User) -> int:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.bonus_presentations


def button(messenger: FakeMessenger, label: str) -> str:
    """Действие кнопки с подписью ``label`` под последним сообщением с ней."""
    for sent in reversed(messenger.texts):
        if sent.keyboard is None:
            continue
        for row in sent.keyboard.rows:
            for found in row:
                if found.text == label and found.action:
                    return found.action
    raise AssertionError(f"нет кнопки «{label}»")


async def build_once(deps: Deps, messenger: FakeMessenger) -> None:
    """Тема → экран параметров → «Собрать»: первая колода доставлена."""
    await handle(deps, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(deps, incoming(text=TOPIC))
    await handle(deps, incoming(action=button(messenger, texts.BUTTON_DECK_BUILD)))


async def test_the_three_buttons_stand_under_the_result_in_order(
    enabled: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    await storage.add_bonus(user.id, presentations=1)

    await build_once(enabled, messenger)

    result = messenger.last_text
    assert result.text == texts.presentation_result(with_pdf=True).text
    assert result.keyboard is not None
    assert [[b.text for b in row] for row in result.keyboard.rows] == [
        [label] for label in LABELS
    ]
    actions = [row[0].action for row in result.keyboard.rows]
    assert actions[2] == Action.PRESENTATION_AGAIN
    assert actions[1] is not None and parse_report_from_action(actions[1])


def test_max_draws_the_three_buttons_in_the_same_order() -> None:
    """В MAX — те же строки; адаптер добавляет только «☰ В меню» в конец."""
    from app.core.scenarios import keyboards

    keyboard = keyboards.presentation_result("report-token", "regen-token")

    attachment = max_keyboards.build(keyboard, show_menu=True)

    assert attachment is not None
    rows = attachment.payload.buttons
    assert [[b.text for b in row] for row in rows][:3] == [[label] for label in LABELS]


async def test_regenerating_builds_the_same_deck_and_charges_like_a_build(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Параметры те же; бесплатная перегенерация уже потрачена — списание."""
    await storage.mark_free_regen_used(user.id, enabled.now())
    await storage.add_bonus(user.id, presentations=2)
    await build_once(enabled, messenger)
    assert await left(storage, user) == 1

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 2
    assert presentations.requests[1] == presentations.requests[0]
    assert await left(storage, user) == 0
    assert TOPIC not in repr(await storage.get_user_by_id(user.id))


async def test_the_first_free_deck_regenerates_once_for_free(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Бесплатный тариф: одна презентация разово и одна перегенерация даром."""
    await storage.add_bonus(user.id, presentations=1)
    await build_once(enabled, messenger)
    assert await left(storage, user) == 0

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 2
    assert await left(storage, user) == 0

    # Вторая перегенерация уже не бесплатная — презентаций нет, пейволл.
    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 2
    assert messenger.last_text.text.startswith("Это была бесплатная презентация")


async def test_the_free_regeneration_is_once_per_person(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Следующая бесплатная колода (за друга) перегенерируется уже за списание."""
    await storage.add_bonus(user.id, presentations=1)
    await build_once(enabled, messenger)
    await handle(enabled, incoming(action=button(messenger, LABELS[0])))
    await storage.add_bonus(user.id, presentations=2)
    await handle(enabled, incoming(action=Action.PRESENTATION_AGAIN))
    await handle(enabled, incoming(text=TOPIC))
    await handle(enabled, incoming(action=button(messenger, texts.BUTTON_DECK_BUILD)))
    assert await left(storage, user) == 1

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 4
    assert await left(storage, user) == 0


async def test_a_failed_free_regeneration_keeps_it_free(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Сбой сборки бесплатную перегенерацию не сжигает: «Повторить» — тоже даром."""
    from app.ports.presentations import PresentationError

    await storage.add_bonus(user.id, presentations=1)
    await build_once(enabled, messenger)
    presentations.errors = [PresentationError("INTERNAL")]

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))
    await handle(enabled, incoming(action=Action.PRESENTATION_RETRY))

    assert len(presentations.requests) == 3
    assert messenger.last_text.text == texts.presentation_result(with_pdf=True).text
    assert await left(storage, user) == 0


async def test_regenerating_without_the_draft_answers_honestly(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Перезапуск — черновика нет: «параметры устарели» и выход в презентации."""
    await storage.add_bonus(user.id, presentations=2)
    await build_once(enabled, messenger)
    regenerate = button(messenger, LABELS[0])
    restarted = replace(enabled, drafts=MemoryHandoff())

    await handle(restarted, incoming(action=regenerate))

    assert len(presentations.requests) == 1
    assert messenger.last_text.text == texts.deck_gone().text
    assert await left(storage, user) == 1


async def test_regenerating_with_nothing_left_shows_the_paywall(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    await storage.mark_free_regen_used(user.id, enabled.now())
    await storage.add_bonus(user.id, presentations=1)
    await build_once(enabled, messenger)

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 1
    assert messenger.last_text.text.startswith("Это была бесплатная презентация")


async def test_buttons_under_older_results_still_work(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    llm: object,
) -> None:
    """«📑 Сделать доклад по презентации» и «Ещё одну презентацию» из прошлых
    сообщений несут те же действия, что и новые кнопки."""
    await storage.add_bonus(user.id, presentations=1, documents=1)
    await build_once(enabled, messenger)
    report = button(messenger, LABELS[1])

    await handle(enabled, incoming(action=report))
    assert messenger.documents_sent, "доклад по презентации сделан"

    await storage.add_bonus(user.id, presentations=1)
    await handle(enabled, incoming(action=Action.PRESENTATION_AGAIN))
    assert messenger.last_text.text == texts.presentation_ask().text


async def test_the_first_deck_mark_survives_in_storage(
    enabled: Deps, user: User, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    """Первая доставленная колода отмечается один раз — вторая уже не первая."""
    assert await storage.mark_first_deck(user.id, clock.now) is True
    assert await storage.mark_first_deck(user.id, clock.now) is False


async def test_a_free_regeneration_leaves_the_balance_alone(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Остаток есть — бесплатная перегенерация его не трогает, следующая тратит."""
    await storage.add_bonus(user.id, presentations=2)
    await build_once(enabled, messenger)
    assert await left(storage, user) == 1

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))
    assert await left(storage, user) == 1

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))
    assert await left(storage, user) == 0
    assert len(presentations.requests) == 3


async def test_only_the_first_deck_regenerates_for_free(
    enabled: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Первую не перегенерировали — бесплатно перегенерировать вторую нельзя."""
    await storage.add_bonus(user.id, presentations=3)
    await build_once(enabled, messenger)
    await handle(enabled, incoming(action=Action.PRESENTATION_AGAIN))
    await handle(enabled, incoming(text=TOPIC))
    await handle(enabled, incoming(action=button(messenger, texts.BUTTON_DECK_BUILD)))
    assert await left(storage, user) == 1

    await handle(enabled, incoming(action=button(messenger, LABELS[0])))

    assert len(presentations.requests) == 3
    assert await left(storage, user) == 0
