"""Кнопки продолжения под результатом (сессия 8, Ч1–Ч3).

Под ответом чата — «Объясни проще», «Короче», «Нарисуй к этому»; под
картинкой — «Ещё вариант», «Другой прикол», «Поделиться»; под приколом —
«Ещё раз», «Другой прикол», «Другу». Каждое нажатие — обычный запрос:
списание только после доставки, без остатка — пейволл. Кнопка под ответом,
которого бот уже не помнит, отвечает честно и даёт выход.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind
from app.core.models import Chat, ChatTurn, IncomingMessage, MessengerKind, Role, User
from app.core.router import handle
from app.core.scenarios import spending
from app.core.scenarios.deps import Deps, Session
from app.infra.antiflood import FloodGuard
from config.prompt import DRAW_PROMPT, SHORTER_PROMPT, SIMPLER_PROMPT
from tests.fakes import FakeImages, FakeLLM, FakeLogger, FakeMessenger, use_up_norm

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")
QUESTION = "Почему небо голубое?"
ANSWER = "Свет рассеивается на молекулах воздуха, синий — сильнее всего."
FOLLOWUPS = [texts.BUTTON_SIMPLER, texts.BUTTON_SHORTER, texts.BUTTON_DRAW_THIS]


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


def labels_under(messenger: FakeMessenger) -> list[str]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [b.text for row in keyboard.rows for b in row]


def action_of(messenger: FakeMessenger, label: str, index: int = -1) -> str:
    sent = [t for t in messenger.texts if t.keyboard is not None][index]
    assert sent.keyboard is not None
    for row in sent.keyboard.rows:
        for button in row:
            if button.text == label and button.action is not None:
                return button.action
    raise AssertionError(f"нет кнопки «{label}»")


async def left(deps: Deps, session: Session, kind: LimitKind) -> int:
    return (await spending.current_allowance(deps, session, kind)).total_left


async def ask(deps: Deps, llm: FakeLLM) -> None:
    llm.answer = ANSWER
    await handle(deps, incoming(text=QUESTION))


# --- Ч1: кнопки стоят под каждым результатом ----------------------------


async def test_a_chat_answer_carries_the_three_followups(
    deps: Deps, user: User, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    await ask(deps, llm)

    assert labels_under(messenger) == FOLLOWUPS


async def test_continue_and_new_dialog_stay_when_they_are_due(
    deps: Deps, user: User, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    deps = replace(deps, settings=replace(deps.settings, new_dialog_after_messages=1))
    llm.truncated = True

    await ask(deps, llm)

    assert labels_under(messenger) == [
        texts.BUTTON_CONTINUE,
        *FOLLOWUPS,
        texts.BUTTON_NEW_DIALOG,
    ]


async def test_a_picture_and_a_preset_carry_their_buttons(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    from app.core.models import Photo
    from app.core.scenarios import images, presets
    from config.presets import PRESETS
    from tests.fakes import PNG_BYTES

    await images.draw(deps, session, "кот-космонавт")
    await presets.apply(deps, session, PRESETS["lego"], [Photo(data=PNG_BYTES)])

    picture, preset = (edit.keyboard for edit in messenger.photo_edits)
    assert picture is not None and preset is not None
    assert [[b.text for b in row] for row in picture.rows] == [
        [texts.BUTTON_ANOTHER_VARIANT, texts.BUTTON_ANOTHER_PRESET],
        [texts.BUTTON_SHARE],
    ]
    assert [[b.text for b in row] for row in preset.rows] == [
        [texts.BUTTON_DRAW_AGAIN, texts.BUTTON_ANOTHER_PRESET],
        [texts.BUTTON_TO_FRIEND],
    ]
    assert [b.action for row in picture.rows for b in row] == [
        Action.IMAGE_AGAIN,
        Action.PRESET_ANOTHER,
        Action.IMAGE_SHARE,
    ]


async def test_another_preset_under_a_picture_opens_the_list(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    await handle(deps, incoming(action=Action.PRESET_ANOTHER))

    assert messenger.last_text.text == texts.PRESETS_ASK


# --- Ч2: обычный запрос, списание после доставки -----------------------


@pytest.mark.parametrize(
    ("label", "prompt"),
    [(texts.BUTTON_SIMPLER, SIMPLER_PROMPT), (texts.BUTTON_SHORTER, SHORTER_PROMPT)],
)
async def test_simpler_and_shorter_cost_a_message(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    messenger: FakeMessenger,
    storage: InMemoryStorage,
    label: str,
    prompt: str,
) -> None:
    await ask(deps, llm)
    before = await left(deps, session, LimitKind.MESSAGES)
    llm.answer = "Попроще: синий свет разлетается сильнее."

    await handle(deps, incoming(action=action_of(messenger, label)))

    turns, _ = llm.calls[-1]
    assert turns[-2:] == (
        ChatTurn(Role.ASSISTANT, ANSWER),
        ChatTurn(Role.USER, prompt),
    )
    assert turns[0] == ChatTurn(Role.USER, QUESTION)
    assert messenger.last_text.text == "Попроще: синий свет разлетается сильнее."
    assert labels_under(messenger) == FOLLOWUPS
    assert await left(deps, session, LimitKind.MESSAGES) == before - 1
    dialog = await storage.get_dialog(session.user.id)
    assert dialog.turns[-2:] == (
        ChatTurn(Role.USER, prompt),
        ChatTurn(Role.ASSISTANT, "Попроще: синий свет разлетается сильнее."),
    )


async def test_draw_this_costs_a_picture_not_a_message(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    await ask(deps, llm)
    messages = await left(deps, session, LimitKind.MESSAGES)
    pictures = await left(deps, session, LimitKind.IMAGES)

    await handle(deps, incoming(action=action_of(messenger, texts.BUTTON_DRAW_THIS)))

    description, _ = images_.generated[0]
    assert description == DRAW_PROMPT.format(text=ANSWER)
    assert len(messenger.photo_edits) == 1
    assert await left(deps, session, LimitKind.IMAGES) == pictures - 1
    assert await left(deps, session, LimitKind.MESSAGES) == messages


async def test_a_failed_followup_spends_nothing_and_can_be_retried(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    await ask(deps, llm)
    before = await left(deps, session, LimitKind.MESSAGES)
    simpler = action_of(messenger, texts.BUTTON_SIMPLER)
    llm.error = RuntimeError("провайдер лёг")

    await handle(deps, incoming(action=simpler))

    assert messenger.last_text.text == texts.CHAT_ERROR
    assert await left(deps, session, LimitKind.MESSAGES) == before
    retry = action_of(messenger, texts.BUTTON_RETRY)
    assert retry == simpler

    llm.error = None
    await handle(deps, incoming(action=retry))
    assert await left(deps, session, LimitKind.MESSAGES) == before - 1


async def test_without_messages_simpler_shows_the_paywall(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    await ask(deps, llm)
    simpler = action_of(messenger, texts.BUTTON_SIMPLER)
    await use_up_norm_messages(deps, session)
    calls = len(llm.calls)

    await handle(deps, incoming(action=simpler))

    assert len(llm.calls) == calls
    assert texts.BUTTON_OPEN_TARIFFS in labels_under(messenger)


async def use_up_norm_messages(deps: Deps, session: Session) -> None:
    usage_left = await left(deps, session, LimitKind.MESSAGES)
    for _ in range(usage_left):
        await spending.charge(deps, session, LimitKind.MESSAGES)


async def test_without_pictures_draw_this_shows_the_paywall(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    llm: FakeLLM,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    await ask(deps, llm)
    draw = action_of(messenger, texts.BUTTON_DRAW_THIS)
    assert await storage.spend_bonus(session.user.id, images=session.user.bonus_images)
    await use_up_norm(deps, session, LimitKind.IMAGES)

    await handle(deps, incoming(action=draw))

    assert images_.generated == []
    assert texts.BUTTON_OPEN_TARIFFS in labels_under(messenger)
    assert texts.IMAGE_DRAWING not in messenger.texts_said()


async def test_two_presses_at_once_give_one_answer_and_one_charge(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    live = replace(deps, guard=FloodGuard(limit=1))
    await ask(live, llm)
    before = await left(live, session, LimitKind.MESSAGES)
    press = incoming(action=action_of(messenger, texts.BUTTON_SIMPLER))
    llm.gate = asyncio.Event()

    first = asyncio.create_task(handle(live, press))
    await llm.entered.get()
    # Без ограничителя второе нажатие повисло бы у тех же ворот — тест
    # должен упасть, а не зависнуть.
    await asyncio.wait_for(handle(live, press), timeout=1)
    llm.gate.set()
    await first

    assert len(llm.calls) == 2  # вопрос и одно «проще»
    assert await left(live, session, LimitKind.MESSAGES) == before - 1
    assert texts.STILL_WORKING in messenger.texts_said()


async def test_drawing_waits_for_no_answer_in_progress(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    messenger: FakeMessenger,
    images_: FakeImages,
) -> None:
    """Картинка стоит картинки: её очередь — общая с картинками, не с чатом."""
    live = replace(deps, guard=FloodGuard(limit=1))
    await ask(live, llm)
    simpler = incoming(action=action_of(messenger, texts.BUTTON_SIMPLER))
    draw = incoming(action=action_of(messenger, texts.BUTTON_DRAW_THIS))
    llm.gate = asyncio.Event()

    answering = asyncio.create_task(handle(live, simpler))
    await llm.entered.get()
    await asyncio.wait_for(handle(live, draw), timeout=1)
    llm.gate.set()
    await answering

    assert len(images_.generated) == 1
    assert texts.STILL_WORKING not in messenger.texts_said()


# --- Ч3: кнопка под ответом, которого уже нет ---------------------------


async def test_a_button_under_a_forgotten_answer_answers_honestly(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    await ask(deps, llm)
    buttons = {label: action_of(messenger, label) for label in FOLLOWUPS}
    await handle(deps, incoming(action=Action.CHAT_NEW_DIALOG))
    calls = len(llm.calls)
    messages = await left(deps, session, LimitKind.MESSAGES)

    for action in buttons.values():
        await handle(deps, incoming(action=action))
        assert messenger.last_text.text == texts.CHAT_ANSWER_GONE
        assert messenger.last_text.show_menu

    assert len(llm.calls) == calls
    assert await left(deps, session, LimitKind.MESSAGES) == messages


async def test_a_button_under_an_older_answer_still_works(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    """Ответ ещё в памяти разговора — кнопка под ним работает по нему."""
    await ask(deps, llm)
    simpler = action_of(messenger, texts.BUTTON_SIMPLER)
    llm.answer = "Другой ответ."
    await handle(deps, incoming(text="А трава почему зелёная?"))

    await handle(deps, incoming(action=simpler))

    turns, _ = llm.calls[-1]
    assert turns[-2] == ChatTurn(Role.ASSISTANT, ANSWER)
    assert ChatTurn(Role.ASSISTANT, "Другой ответ.") not in turns


async def test_the_buttons_under_a_cut_answer_survive_its_continuation(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    """Продолжение склеивается с началом — кнопки под началом не теряются."""
    llm.truncated = True
    llm.answer = "Начало длинного ответа " * 20
    await handle(deps, incoming(text=QUESTION))
    under_start = action_of(messenger, texts.BUTTON_SHORTER)
    llm.truncated = False
    llm.answer = "и его конец."
    await handle(deps, incoming(action=Action.CHAT_CONTINUE))
    under_end = action_of(messenger, texts.BUTTON_SHORTER)

    assert under_start == under_end
    await handle(deps, incoming(action=under_start))
    turns, _ = llm.calls[-1]
    assert turns[-2] == ChatTurn(
        Role.ASSISTANT, "Начало длинного ответа " * 20 + "и его конец."
    )


# --- О2: текста ответов и запросов нет в логах -------------------------


async def test_followups_log_no_text(
    deps: Deps,
    session: Session,
    llm: FakeLLM,
    messenger: FakeMessenger,
    logger: FakeLogger,
) -> None:
    await ask(deps, llm)
    for label in FOLLOWUPS:
        await handle(deps, incoming(action=action_of(messenger, label, index=0)))
    llm.error = RuntimeError("сбой")
    await handle(
        deps, incoming(action=action_of(messenger, texts.BUTTON_SIMPLER, index=0))
    )

    logged = str([(e.event, e.fields) for e in logger.events])
    for secret in ("небо", "голубое", "рассеивается", "Попроще"):
        assert secret not in logged


def test_a_damaged_button_is_not_a_followup() -> None:
    """Данные кнопки приходят снаружи: чужой вид действия — не наша кнопка."""
    from app.core.actions import Followup, followup_action, parse_followup_action

    assert parse_followup_action("c:f:x:0123456789ab") is None
    assert parse_followup_action("c:f:s:") is None
    assert parse_followup_action(followup_action(Followup.DRAW, "ab12")) is not None
