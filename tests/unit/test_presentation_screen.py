"""Экран параметров перед сборкой презентации (сессия 7, В2, В4, В5, В6).

Все пути — по теме, «Придумай сам», по докладу — ведут на один экран: он
приходит заполненным, сборка запускается одной кнопкой, любой параметр можно
поменять и вернуться. Значения — только те, что принимает API (docs/API.md).

Тема и материал живут в памяти по жетону, а не в базе: кнопки экрана несут
только жетон. Устарел жетон — кнопка честно об этом говорит.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action, DeckField, deck_go_action, deck_set_action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from app.ports.presentations import (
    AUDIENCES,
    DEFAULT_SLIDES,
    LANGUAGES,
    MAX_SLIDES,
    MIN_SLIDES,
    PresentationError,
)
from tests.fakes import FakeLogger, FakeMessenger, FakePresentations

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
TOPIC = "Управление требованиями стейкхолдеров в ИТ-стартапе"


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


@pytest.fixture
def presentations() -> FakePresentations:
    return FakePresentations()


@pytest.fixture
def enabled(deps: Deps, presentations: FakePresentations) -> Deps:
    return replace(deps, presentations=presentations)


@pytest.fixture
async def owner(storage: InMemoryStorage, user: User) -> User:
    """Две презентации — хватит на повтор после сбоя; доклад — на путь по нему."""
    await storage.add_bonus(user.id, presentations=2, documents=1)
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


def buttons(messenger: FakeMessenger) -> list[tuple[str, str | None]]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [(b.text, b.action) for row in keyboard.rows for b in row]


def action_of(messenger: FakeMessenger, label: str) -> str:
    for text, action in buttons(messenger):
        if text == label:
            assert action is not None
            return action
    raise AssertionError(f"нет кнопки «{label}»: {buttons(messenger)}")


async def press(enabled: Deps, messenger: FakeMessenger, label: str) -> None:
    await handle(enabled, incoming(action=action_of(messenger, label)))


async def to_screen(enabled: Deps) -> None:
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, incoming(text=TOPIC))


def screen_lines(messenger: FakeMessenger) -> list[str]:
    return messenger.last_text.text.split("\n")


# --- В2: экран заполнен и собирает одной кнопкой --------------------------


async def test_the_screen_comes_filled_with_the_defaults(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    await to_screen(enabled)

    assert screen_lines(messenger) == [
        "📋 Проверь параметры",
        f"📝 Тема: {TOPIC}",
        "📂 Тип: Доклад",
        "📎 Материал: нет — соберём по теме",
        "🌐 Язык: 🇷🇺 Русский",
        "🔢 Слайдов: 9 (вместе с титульным и финальным)",
        "👥 Аудитория: 👥 Широкая",
        "🎨 Дизайн: Графит светлая",
    ]
    assert buttons(messenger)[0][0] == texts.BUTTON_DECK_BUILD


async def test_one_button_builds_with_the_shown_parameters(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    await to_screen(enabled)

    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )

    request = presentations.requests[0]
    assert (request.topic, request.language, request.slides) == (TOPIC, "ru", 9)
    assert (request.audience, request.theme_id) == ("general", "graphite_light")
    assert messenger.timeline[-1] == ("text", texts.PRESENTATION_RESULT)


@pytest.mark.parametrize(
    ("field_button", "option", "line", "attribute", "value"),
    [
        (
            texts.BUTTON_DECK_LANGUAGE,
            texts.LANGUAGE_LABELS["en"],
            "🌐 Язык: 🇬🇧 Английский",
            "language",
            "en",
        ),
        (
            texts.BUTTON_DECK_SLIDES,
            "12",
            "🔢 Слайдов: 12 (вместе с титульным и финальным)",
            "slides",
            12,
        ),
        (
            texts.BUTTON_DECK_AUDIENCE,
            texts.AUDIENCE_LABELS["investors"],
            "👥 Аудитория: 💰 Инвесторы",
            "audience",
            "investors",
        ),
        (
            texts.BUTTON_DECK_DESIGN,
            "Лазурь",
            "🎨 Дизайн: Лазурь",
            "theme_id",
            "azure_coral",
        ),
    ],
)
async def test_a_changed_parameter_comes_back_on_the_screen_and_into_the_deck(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    field_button: str,
    option: str,
    line: str,
    attribute: str,
    value: object,
) -> None:
    await to_screen(enabled)
    await handle(enabled, incoming(action=action_of(messenger, field_button)))

    await handle(enabled, incoming(action=action_of(messenger, option)))

    assert line in screen_lines(messenger)
    assert screen_lines(messenger)[0] == "📋 Проверь параметры"
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )
    assert getattr(presentations.requests[0], attribute) == value


async def test_the_topic_can_be_rewritten(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    await to_screen(enabled)
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_TOPIC))
    )

    await handle(enabled, incoming(text="Фотосинтез у растений"))

    assert "📝 Тема: Фотосинтез у растений" in screen_lines(messenger)
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )
    assert presentations.requests[0].topic == "Фотосинтез у растений"


async def test_a_bad_new_topic_keeps_waiting_for_a_good_one(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    await to_screen(enabled)
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_TOPIC))
    )

    await handle(enabled, incoming(text="ну"))
    assert messenger.last_text.text == texts.PRESENTATION_TOPIC_BAD
    await handle(enabled, incoming(text="Фотосинтез у растений"))

    assert "📝 Тема: Фотосинтез у растений" in screen_lines(messenger)


async def test_back_returns_to_the_screen_unchanged(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    await to_screen(enabled)
    shown = messenger.last_text.text
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_LANGUAGE))
    )

    await handle(enabled, incoming(action=action_of(messenger, texts.BUTTON_BACK)))

    assert messenger.last_text.text == shown


async def test_the_choices_are_the_ones_the_api_takes(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Язык, аудитория, число слайдов — из docs/API.md; дизайн — из API."""
    await to_screen(enabled)
    expected = {
        texts.BUTTON_DECK_LANGUAGE: [texts.LANGUAGE_LABELS[code] for code in LANGUAGES],
        texts.BUTTON_DECK_AUDIENCE: [texts.AUDIENCE_LABELS[code] for code in AUDIENCES],
        texts.BUTTON_DECK_SLIDES: [
            str(count) for count in range(MIN_SLIDES, MAX_SLIDES + 1)
        ],
        texts.BUTTON_DECK_DESIGN: [theme.name for theme in presentations.available],
    }
    screen = messenger.last_text.text
    for field_button, options in expected.items():
        await handle(enabled, incoming(action=action_of(messenger, field_button)))
        shown = [
            text.removeprefix(texts.CURRENT_MARK) for text, _ in buttons(messenger)
        ]
        assert shown == [*options, texts.BUTTON_BACK]
        await handle(enabled, incoming(action=action_of(messenger, texts.BUTTON_BACK)))
        assert messenger.last_text.text == screen
    assert (MIN_SLIDES, DEFAULT_SLIDES, MAX_SLIDES) == (4, 9, 20)


async def test_the_type_is_shown_but_offers_nothing_to_change(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    """У API пока один тип — доклад; кнопка без выбора обещала бы лишнее."""
    await to_screen(enabled)

    labels = [text for text, _ in buttons(messenger)]
    assert not [label for label in labels if "Тип" in label]


async def test_a_value_the_api_does_not_take_changes_nothing(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Значение в кнопке приходит снаружи — верить ему нельзя."""
    await to_screen(enabled)
    build = action_of(messenger, texts.BUTTON_DECK_BUILD)
    token = build.rsplit(":", 1)[-1]
    await handle(
        enabled,
        incoming(action=deck_set_action(token, DeckField.DESIGN, "azure_coral")),
    )

    for field, value in (
        (DeckField.LANGUAGE, "fr"),
        (DeckField.SLIDES, "99"),
        (DeckField.AUDIENCE, "aliens"),
        (DeckField.DESIGN, "no_such_theme"),
    ):
        await handle(enabled, incoming(action=deck_set_action(token, field, value)))
    await handle(enabled, incoming(action=build))

    request = presentations.requests[0]
    assert (request.language, request.slides) == ("ru", 9)
    assert (request.audience, request.theme_id) == ("general", "azure_coral")


async def test_suggest_lands_on_the_screen(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    await handle(enabled, incoming(action=Action.PRESENTATION_SUGGEST))

    assert screen_lines(messenger)[0] == "📋 Проверь параметры"
    assert screen_lines(messenger)[1].startswith("📝 Тема: ")


async def test_a_report_lands_on_the_screen_as_material(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    await handle(enabled, incoming(action="d:pick:topic_report"))
    await handle(enabled, incoming(text="Фотосинтез у растений"))
    await handle(
        enabled,
        incoming(action=action_of(messenger, texts.BUTTON_PRESENTATION_FROM_REPORT)),
    )

    assert "📎 Материал: доклад — соберём по нему" in screen_lines(messenger)
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )
    assert presentations.requests[0].material


async def test_a_deck_from_the_report_uses_up_its_button(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    """Колода по докладу доставлена — кнопка под докладом честно говорит, что всё."""
    await handle(enabled, incoming(action="d:pick:topic_report"))
    await handle(enabled, incoming(text="Фотосинтез у растений"))
    from_report = action_of(messenger, texts.BUTTON_PRESENTATION_FROM_REPORT)
    await handle(enabled, incoming(action=from_report))
    await press(enabled, messenger, texts.BUTTON_DECK_BUILD)

    await handle(enabled, incoming(action=from_report))

    assert messenger.last_text.text == (
        texts.link_already_used(texts.MENU_PRESENTATIONS).text
    )


# --- В4: норма и списание ---------------------------------------------------


async def test_a_deck_costs_one_only_after_delivery(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
) -> None:
    await to_screen(enabled)
    presentations.errors = [PresentationError("RENDER_FAILED")]

    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )
    assert (await _left(storage, owner)) == 2
    await handle(enabled, incoming(action=Action.PRESENTATION_RETRY))

    assert (await _left(storage, owner)) == 1
    assert [r.topic for r in presentations.requests] == [TOPIC, TOPIC]


async def test_two_presses_at_once_build_one_deck(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
) -> None:
    await to_screen(enabled)
    build = action_of(messenger, texts.BUTTON_DECK_BUILD)

    await asyncio.gather(
        handle(enabled, incoming(action=build)), handle(enabled, incoming(action=build))
    )

    assert len(presentations.requests) == 1
    assert (await _left(storage, owner)) == 1


async def test_pressing_build_again_after_the_deck_makes_no_second(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    await to_screen(enabled)
    build = action_of(messenger, texts.BUTTON_DECK_BUILD)
    await handle(enabled, incoming(action=build))

    await handle(enabled, incoming(action=build))

    assert len(presentations.requests) == 1
    assert messenger.last_text.text == texts.deck_already_built().text
    assert buttons(messenger) == [(texts.MENU_PRESENTATIONS, Action.MENU_PRESENTATIONS)]


# --- В5: старая кнопка без данных ------------------------------------------


@pytest.mark.parametrize(
    "stale",
    [
        deck_go_action("0123456789abcdef"),
        deck_set_action("0123456789abcdef", DeckField.LANGUAGE, "en"),
        "v:opt:0123456789abcdef:l",
        "v:back:0123456789abcdef",
    ],
)
async def test_a_screen_button_without_data_answers_honestly(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    stale: str,
) -> None:
    await handle(enabled, incoming(action=stale))

    assert messenger.last_text.text == texts.deck_gone().text
    assert buttons(messenger) == [(texts.MENU_PRESENTATIONS, Action.MENU_PRESENTATIONS)]
    assert presentations.requests == []


async def test_an_old_theme_button_answers_honestly(
    enabled: Deps, owner: User, messenger: FakeMessenger
) -> None:
    """Кнопка оформления из версии до экрана параметров."""
    await handle(enabled, incoming(action="v:theme:azure_coral"))

    assert messenger.last_text.text == texts.deck_gone().text


# --- В6: темы нет ни в базе, ни в логах ------------------------------------


async def test_the_topic_never_reaches_the_database_or_the_log(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    storage: InMemoryStorage,
    logger: FakeLogger,
) -> None:
    await to_screen(enabled)
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_TOPIC))
    )
    after_edit = await storage.get_user_by_id(owner.id)
    presentations.errors = [PresentationError("RENDER_FAILED")]
    await handle(enabled, incoming(text="Секретная тема доклада"))
    await handle(
        enabled, incoming(action=action_of(messenger, texts.BUTTON_DECK_BUILD))
    )

    user = await storage.get_user_by_id(owner.id)
    assert user is not None and after_edit is not None
    stored = f"{user.pending} {user.retry_context} {after_edit.pending}"
    assert "Секретная" not in stored and "стейкхолдеров" not in stored
    logged = str([(e.event, e.fields) for e in logger.events])
    assert "Секретная" not in logged and "стейкхолдеров" not in logged


async def _left(storage: InMemoryStorage, user: User) -> int:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.bonus_presentations
