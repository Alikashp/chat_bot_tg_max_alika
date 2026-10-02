"""Презентации: путь человека от кнопки до файлов (фаза 10, К1–К9).

Провайдер здесь фейковый: как адаптер разговаривает с API, проверяет
tests/unit/test_presentations_api.py. Здесь — то, что видит человек, и
главный инвариант: презентация списывается только после доставки файла.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import pending, texts
from app.core.actions import Action, theme_action
from app.core.generations import GenerationKind, GenerationStatus
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios import keyboards, onboarding
from app.core.scenarios.deps import Deps
from app.ports.presentations import (
    PresentationBusyError,
    PresentationError,
    PresentationTimeoutError,
)
from tests.fakes import (
    PDF_BYTES,
    PPTX_BYTES,
    FakeLogger,
    FakeMessenger,
    FakePresentations,
    FrozenClock,
)

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
TOPIC = "Как работает фотосинтез"


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


@pytest.fixture
def presentations() -> FakePresentations:
    return FakePresentations()


@pytest.fixture
def enabled(deps: Deps, presentations: FakePresentations) -> Deps:
    """Зависимости с подключённым API презентаций."""
    return replace(deps, presentations=presentations)


@pytest.fixture
async def owner(storage: InMemoryStorage, user: User) -> User:
    """Человек с одной презентацией — той, что выдаётся каждому разово."""
    await storage.add_bonus(user.id, presentations=1)
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


async def left(storage: InMemoryStorage, user: User) -> int:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.bonus_presentations


async def up_to_themes(deps: Deps) -> None:
    """Кнопка меню и тема — до выбора оформления."""
    await handle(deps, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(deps, incoming(text=TOPIC))


def labels_of(messenger: FakeMessenger) -> list[str]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [button.text for row in keyboard.rows for button in row]


def last_edit_buttons(messenger: FakeMessenger) -> list[tuple[str, str | None]]:
    keyboard = messenger.text_edits[-1].keyboard
    assert keyboard is not None
    return [(b.text, b.action) for row in keyboard.rows for b in row]


# --- К1, К2: кнопка в меню только вместе с ключом ------------------------


def test_the_menu_has_presentations_only_when_they_are_switched_on() -> None:
    """К1 и К2: Картинки · Доклад · Презентации · Профиль · Тарифы."""
    on = [[b.text for b in row] for row in keyboards.main_menu(presentations=True).rows]
    off = [[b.text for b in row] for row in keyboards.main_menu().rows]

    assert on == [
        [texts.MENU_IMAGES, texts.MENU_DOCUMENTS],
        [texts.MENU_PRESENTATIONS],
        [texts.MENU_PROFILE, texts.MENU_TARIFFS],
    ]
    assert texts.MENU_PRESENTATIONS not in [label for row in off for label in row]


async def test_the_menu_screen_follows_the_switch(
    deps: Deps, enabled: Deps, user: User, messenger: FakeMessenger
) -> None:
    """В MAX меню открывается отдельным сообщением — и оно тоже знает о ключе."""
    await handle(enabled, incoming(action=Action.MENU_SHOW))
    assert texts.MENU_PRESENTATIONS in labels_of(messenger)

    await handle(deps, incoming(action=Action.MENU_SHOW))
    assert texts.MENU_PRESENTATIONS not in labels_of(messenger)
    assert "резентац" not in messenger.last_text.text


async def test_without_the_api_a_stale_button_leads_nowhere_bad(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Кнопка из меню, пришедшего до снятия ключа, не роняет бота и не врёт."""
    await handle(deps, incoming(action=Action.MENU_PRESENTATIONS))

    assert messenger.last_text.text == texts.unsupported_input().text


async def test_without_the_api_the_referral_promises_no_presentation(
    deps: Deps, enabled: Deps, user: User, messenger: FakeMessenger
) -> None:
    """К2: награда за друга называет презентацию только там, где её дают."""
    await handle(deps, incoming(action=Action.MY_LINK))
    assert "резентац" not in messenger.last_text.text

    await handle(enabled, incoming(action=Action.MY_LINK))
    assert "+1 презентация" in messenger.last_text.text


# --- К3: путь целиком ----------------------------------------------------


async def test_the_whole_path_from_button_to_files(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """К3: кнопка → тема → оформление → «готовлю» → PDF и PPTX → «Ещё одну»."""
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    assert messenger.last_text.text == texts.PRESENTATION_ASK

    await handle(enabled, incoming(text=TOPIC))
    assert messenger.last_text.text == texts.PRESENTATION_PICK_THEME
    # Оформления — те, что прислал провайдер, а не зашитые у нас.
    assert labels_of(messenger)[:2] == ["Графит светлая", "Лазурь"]

    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert texts.PRESENTATION_WORKING in messenger.texts_said()
    assert presentations.built == [(TOPIC, "azure_coral")]
    assert [d.data for d in messenger.documents_sent] == [PDF_BYTES, PPTX_BYTES]
    assert [d.filename for d in messenger.documents_sent] == [
        f"{TOPIC}.pdf",
        f"{TOPIC}.pptx",
    ]
    assert messenger.text_edits[-1].text == texts.PRESENTATION_READY
    assert last_edit_buttons(messenger) == [
        (texts.BUTTON_PRESENTATION_AGAIN, Action.PRESENTATION_AGAIN)
    ]
    assert await left(storage, owner) == 0


async def test_one_more_starts_over(
    enabled: Deps, owner: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """«Ещё одну» начинает с темы, а не повторяет прошлую колоду."""
    await storage.add_bonus(owner.id, presentations=1)
    await up_to_themes(enabled)
    await handle(enabled, incoming(action=theme_action("azure_coral")))

    await handle(enabled, incoming(action=Action.PRESENTATION_AGAIN))

    assert messenger.last_text.text == texts.PRESENTATION_ASK


@pytest.mark.parametrize("topic", ["ок", "  ок  ", "я" * 201])
async def test_a_topic_out_of_bounds_is_refused_before_the_api(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    topic: str,
) -> None:
    """К3: тема короче 3 или длиннее 200 знаков до провайдера не доходит."""
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(enabled, incoming(text=topic))

    assert messenger.last_text.text == texts.PRESENTATION_TOPIC_BAD
    assert presentations.themes_calls == 0
    assert presentations.built == []
    # Ожидание темы остаётся: человек просто напишет её ещё раз.
    fresh = await storage.get_user_by_id(owner.id)
    assert fresh is not None
    assert fresh.pending == pending.AWAIT_PRESENTATION_TOPIC


async def test_bounds_are_inclusive(
    enabled: Deps, owner: User, presentations: FakePresentations
) -> None:
    """Ровно три и ровно двести знаков — допустимая тема."""
    for topic in ("ИИ!", "я" * 200):
        await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))
        await handle(enabled, incoming(text=topic))

    assert presentations.themes_calls == 2


# --- К4: списание только после доставки ----------------------------------


@pytest.mark.parametrize(
    "error",
    [PresentationError("INTERNAL"), PresentationTimeoutError("CLIENT_TIMEOUT")],
    ids=["сбой API", "дольше пяти минут"],
)
async def test_a_failed_build_spends_nothing_and_offers_retry(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    error: Exception,
) -> None:
    """К4: сбой API или ожидание дольше пяти минут — не списана, есть «Повторить»."""
    presentations.errors = [error]
    await up_to_themes(enabled)

    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert messenger.documents_sent == []
    assert messenger.text_edits[-1].text == texts.PRESENTATION_ERROR
    assert last_edit_buttons(messenger) == [
        (texts.BUTTON_RETRY, Action.PRESENTATION_RETRY)
    ]
    assert await left(storage, owner) == 1


async def test_retry_builds_the_same_topic_and_theme(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """«Повторить» не заставляет писать тему заново."""
    presentations.errors = [PresentationError("INTERNAL")]
    await up_to_themes(enabled)
    await handle(enabled, incoming(action=theme_action("azure_coral")))

    await handle(enabled, incoming(action=Action.PRESENTATION_RETRY))

    assert presentations.built == [(TOPIC, "azure_coral")] * 2
    assert len(messenger.documents_sent) == 2
    assert await left(storage, owner) == 0


async def test_a_failed_delivery_spends_nothing(
    enabled: Deps, owner: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """К4: файл собран, но не доехал — человек его не получил и не платит."""
    messenger.fail_send_document = RuntimeError("мессенджер не принял файл")
    await up_to_themes(enabled)

    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert messenger.text_edits[-1].text == texts.PRESENTATION_ERROR
    assert last_edit_buttons(messenger) == [
        (texts.BUTTON_RETRY, Action.PRESENTATION_RETRY)
    ]
    assert await left(storage, owner) == 1


async def test_a_missing_pdf_still_delivers_the_pptx(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """К4: PDF не собрался — уходит PPTX, и это доставка."""
    presentations.pdf = None
    await up_to_themes(enabled)

    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert [d.data for d in messenger.documents_sent] == [PPTX_BYTES]
    assert messenger.text_edits[-1].text == texts.PRESENTATION_READY_WITHOUT_PDF
    assert await left(storage, owner) == 0


async def test_without_presentations_left_the_invite_screen_shows(
    enabled: Deps,
    user: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Закончились — экран с «Позвать друга», а не тема в пустоту."""
    await handle(enabled, incoming(action=Action.MENU_PRESENTATIONS))

    assert messenger.last_text.text == texts.paywall_presentations(1).text
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert [(b.text, b.action) for row in keyboard.rows for b in row] == [
        (texts.button_invite_for_presentations(1), Action.INVITE_FRIEND)
    ]
    assert presentations.themes_calls == 0


# --- К5: одна колода на нажатие ------------------------------------------


async def test_a_double_press_builds_one_deck(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """К5: два нажатия разом — одна колода и одно списание."""
    await storage.add_bonus(owner.id, presentations=1)
    await up_to_themes(enabled)

    press = incoming(action=theme_action("azure_coral"))
    await asyncio.gather(handle(enabled, press), handle(enabled, press))

    assert presentations.built == [(TOPIC, "azure_coral")]
    assert await left(storage, owner) == 1
    assert texts.PRESENTATION_IN_PROGRESS in messenger.texts_said()


async def test_a_late_second_press_builds_nothing(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    presentations: FakePresentations,
) -> None:
    """Второе нажатие после готовой колоды не собирает её заново."""
    await storage.add_bonus(owner.id, presentations=1)
    await up_to_themes(enabled)
    press = incoming(action=theme_action("azure_coral"))

    await handle(enabled, press)
    await handle(enabled, press)

    assert presentations.built == [(TOPIC, "azure_coral")]
    assert await left(storage, owner) == 1


async def test_one_person_has_one_build_at_a_time(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """К5: пока идёт сборка, вторая не начинается — даже по «Повторить»."""
    await storage.add_bonus(owner.id, presentations=5)
    presentations.errors = [PresentationError("INTERNAL")]
    await up_to_themes(enabled)
    await handle(enabled, incoming(action=theme_action("azure_coral")))

    await asyncio.gather(
        handle(enabled, incoming(action=Action.PRESENTATION_RETRY)),
        handle(enabled, incoming(action=Action.PRESENTATION_RETRY)),
    )

    assert presentations.max_running == 1
    assert len(presentations.built) == 2


async def test_a_build_left_hanging_does_not_lock_forever(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    presentations: FakePresentations,
) -> None:
    """Сборку оборвала выкатка — через десять минут человек не заперт."""
    assert await storage.claim_presentation(
        owner.id, clock(), stale_after=timedelta(minutes=10)
    )
    await up_to_themes(enabled)

    clock.advance(minutes=11)
    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert presentations.built == [(TOPIC, "azure_coral")]


# --- К8: перегрузка ------------------------------------------------------


@pytest.mark.parametrize("reached_api", [False, True])
async def test_busy_says_so_and_spends_nothing(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    presentations: FakePresentations,
    reached_api: bool,
) -> None:
    """К8: сверх предела — «много запросов, попробуй позже», без списания."""
    presentations.errors = [PresentationBusyError(reached_api=reached_api)]
    await up_to_themes(enabled)

    await handle(enabled, incoming(action=theme_action("azure_coral")))

    assert messenger.text_edits[-1].text == texts.PRESENTATION_BUSY
    assert last_edit_buttons(messenger) == [
        (texts.BUTTON_RETRY, Action.PRESENTATION_RETRY)
    ]
    assert await left(storage, owner) == 1
    # Свой отказ — без обращения к API, и в учёт он не идёт.
    assert len(storage.generations) == (1 if reached_api else 0)


# --- К9 и тема вне логов -------------------------------------------------


async def test_every_build_is_recorded_without_the_topic(
    enabled: Deps,
    owner: User,
    storage: InMemoryStorage,
    logger: FakeLogger,
    presentations: FakePresentations,
) -> None:
    """К9: каждая сборка — строка в generations; темы нет ни там, ни в логе."""
    await storage.add_bonus(owner.id, presentations=1)
    presentations.errors = [PresentationError("RENDER_FAILED")]
    await up_to_themes(enabled)
    await handle(enabled, incoming(action=theme_action("azure_coral")))
    await handle(enabled, incoming(action=Action.PRESENTATION_RETRY))

    rows = [(g.kind, g.status, g.preset_id) for g in storage.generations]
    assert rows == [
        (GenerationKind.PRESENTATION, GenerationStatus.FAILED, "azure_coral"),
        (GenerationKind.PRESENTATION, GenerationStatus.SUCCESS, "azure_coral"),
    ]
    assert TOPIC not in repr(storage.generations)
    assert TOPIC not in repr(logger.events)
    assert "фотосинтез" not in repr(logger.events)


async def test_a_failed_theme_list_offers_retry(
    enabled: Deps,
    owner: User,
    messenger: FakeMessenger,
    presentations: FakePresentations,
) -> None:
    """Оформления не загрузились — тема не теряется, «Повторить» её помнит."""
    presentations.themes_error = PresentationError("SERVICE_UNAVAILABLE")
    await up_to_themes(enabled)

    assert messenger.last_text.text == texts.PRESENTATION_ERROR

    presentations.themes_error = None
    await handle(enabled, incoming(action=Action.PRESENTATION_RETRY))

    assert messenger.last_text.text == texts.PRESENTATION_PICK_THEME


# --- К6, К7: разовая выдача и награда за друга ---------------------------


async def test_a_new_person_gets_one_presentation(
    enabled: Deps, storage: InMemoryStorage
) -> None:
    """К6: одна презентация каждому — при регистрации."""
    session = await onboarding.start(
        enabled,
        Chat(messenger=MessengerKind.TELEGRAM, chat_id="77"),
        MessengerKind.TELEGRAM,
        "77",
    )

    assert await left(storage, session.user) == 1


async def test_the_referrer_gets_a_presentation_for_a_friend(
    enabled: Deps,
    storage: InMemoryStorage,
    user: User,
    messenger: FakeMessenger,
) -> None:
    """К7: +1 презентация пригласившему; приглашённому — только разовая."""
    session = await onboarding.start(
        enabled,
        Chat(messenger=MessengerKind.TELEGRAM, chat_id="77"),
        MessengerKind.TELEGRAM,
        "77",
        f"ref_{user.referral_code}",
    )

    assert await left(storage, user) == 1
    assert await left(storage, session.user) == 1
    reward = texts.referral_reward(messages=20, images=2, presentations=1).text
    assert reward in messenger.texts_said()
    assert "+1 презентация" in reward


async def test_without_the_api_a_friend_brings_no_presentation(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """К2 и К7: без ключа награда презентацию не обещает и не начисляет."""
    await onboarding.start(
        deps,
        Chat(messenger=MessengerKind.TELEGRAM, chat_id="77"),
        MessengerKind.TELEGRAM,
        "77",
        f"ref_{user.referral_code}",
    )

    assert await left(storage, user) == 0
    assert texts.referral_reward(messages=20, images=2).text in messenger.texts_said()
