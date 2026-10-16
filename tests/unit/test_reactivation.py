"""Цепочка реактивации R1–R3, только Telegram (сессия 12, шаг 3).

Получатель — бесплатный, без отметки «остановил бота», с личной перепиской
в Telegram. Шаг уходит только внутри своего интервала неактивности и только
с 15:00 до 20:00 по Москве; пропущенный не догоняется. Каждый шаг — не
больше раза за цикл неактивности, маркетинг — не чаще раза в 7 дней. Часы в
тестах — UTC, как на сервере: 12:00 UTC — это 15:00 по Москве.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.actions import Action
from app.core.mailing import Mailer
from app.core.models import (
    Chat,
    IncomingMessage,
    MailingStatus,
    MessengerKind,
    TariffId,
    User,
)
from app.core.referral import referral_url
from app.core.router import handle
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, session_for
from app.core.scenarios.reach import WatchedMessenger
from app.ports.messenger import RecipientGoneError
from tests.fakes import (
    FakeCards,
    FakeLLM,
    FakeLogger,
    FakeMessenger,
    FakePresentations,
    FakeStars,
    FrozenClock,
)

R1_TEXT = (
    "Есть тема следующего доклада? Напиши её одним сообщением — через пару "
    "минут пришлю презентацию в PDF и PowerPoint."
)
R2_TEXT = (
    "Дарим тебе ещё одну презентацию 🎁\nДействует 3 дня — успей сделать к семинару."
)


@pytest.fixture
def on(deps: Deps) -> Deps:
    """Рассылка и все её шаги включены, раздел презентаций есть."""
    return replace(
        deps,
        presentations=FakePresentations(),
        settings=replace(
            deps.settings,
            mailings_enabled=True,
            mailing_r1_enabled=True,
            mailing_r2_enabled=True,
            mailing_r3_enabled=True,
        ),
    )


async def quiet(storage: InMemoryStorage, external_id: str = "7") -> User:
    """Бесплатный человек из Telegram, последний раз активный «сейчас»."""
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id=external_id,
        referral_code=f"code{external_id}",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )


def mailer(deps: Deps) -> Mailer:
    return Mailer(deps=deps)


def mailed(messenger: FakeMessenger, chat_id: str = "7") -> list[str]:
    return [sent.text for sent in messenger.texts if sent.chat.chat_id == chat_id]


async def steps(storage: InMemoryStorage, user: User) -> list[tuple[str, str]]:
    return [(row.step, row.status.value) for row in await storage.mailings_of(user.id)]


# --- Р2, Р3: окно часов и интервалы -----------------------------------------


async def test_four_days_quiet_gets_r1_at_four_pm(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Р2: 16:00 по Москве (13:00 UTC), четвёртые сутки — R1 уходит."""
    user = await quiet(storage)
    clock.advance(days=4, hours=1)

    await mailer(on).run()

    assert mailed(messenger) == [R1_TEXT]
    sent = messenger.texts[-1]
    assert sent.keyboard is not None
    assert [[b.text for b in row] for row in sent.keyboard.rows] == [
        ["📊 Написать тему"]
    ]
    assert await steps(storage, user) == [("R1", "sent")]


async def test_nothing_goes_at_ten_pm(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Р2: 22:00 по Москве (19:00 UTC) — вне окна, R1 не уходит."""
    user = await quiet(storage)
    clock.advance(days=4, hours=7)

    await mailer(on).run()

    assert mailed(messenger) == []
    assert await steps(storage, user) == []


async def test_six_days_quiet_gets_no_r1(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Р3: пропущенный R1 не догоняется."""
    await quiet(storage)
    clock.advance(days=6, hours=1)

    await mailer(on).run()

    assert mailed(messenger) == []


async def test_forty_days_quiet_gets_nothing(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    await quiet(storage)
    clock.advance(days=40, hours=1)

    await mailer(on).run()

    assert mailed(messenger) == []


async def test_the_whole_chain_goes_once_each_in_its_interval(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Проход каждый час месяц: R1, R2, R3 — по одному разу, и всё."""
    user = await quiet(storage)

    for _ in range(30 * 24):
        await mailer(on).run()
        clock.advance(hours=1)

    assert [step for step, _ in await steps(storage, user)] == ["R1", "R2", "R3"]
    assert len(mailed(messenger)) == 3


# --- Р4: действие человека начинает цепочку заново --------------------------


async def test_writing_after_r1_restarts_the_chain(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await mailer(on).run()
    clock.advance(hours=1)
    await handle(
        on,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="7"),
            external_user_id="7",
            text="Фотосинтез",
        ),
    )
    restarted = clock.now
    messenger.texts.clear()

    # Через 10 суток от старой даты — уже не R2: человек с тех пор писал.
    clock.advance(days=6)
    await mailer(on).run()
    assert mailed(messenger) == []

    # Через 4 суток от новой даты R1 нового цикла не уходит: с прошлого R1
    # не прошло недели (правило «один маркетинг в 7 дней»), шаг пропадает.
    clock.now = restarted + timedelta(days=4)
    await mailer(on).run()
    assert mailed(messenger) == []

    # R2 нового цикла — через 10 суток от новой даты.
    clock.now = restarted + timedelta(days=10, minutes=1)
    await mailer(on).run()
    assert mailed(messenger) == [R2_TEXT]
    assert [step for step, _ in await steps(storage, user)] == ["R1", "R2"]


# --- Правило 5: не чаще раза в 7 дней ---------------------------------------


async def test_a_step_held_back_by_the_weekly_rule_is_lost(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Маркетинг был вчера — R1 в своём интервале не уходит и не догоняется."""
    user = await quiet(storage)
    await storage.claim_mailing(user.id, "R3", clock.now, clock.now)
    clock.advance(days=3, hours=1)
    await storage.finish_mailing(
        user.id, "R3", user.created_at, status=MailingStatus.SENT, at=clock.now
    )

    for _ in range(3 * 24):
        await mailer(on).run()
        clock.advance(hours=1)

    assert "R1" not in [step for step, _ in await steps(storage, user)]


# --- Р5: платящий и пробный период ------------------------------------------


async def test_paying_people_and_the_trial_get_nothing(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    stars: FakeStars,
) -> None:
    payer = await quiet(storage, "7")
    trial_taker = await quiet(storage, "8")
    await payments.start_stars(on, session_for(on, payer), TariffId.PRO)
    await payments.confirm(on, stars.invoices[-1].order_id, charge_id="c-1")
    trial = replace(
        on,
        cards=FakeCards(recurring=True),
        settings=replace(on.settings, trial_enabled=True),
    )
    await payments.start_trial(trial, session_for(trial, trial_taker))
    assert isinstance(trial.cards, FakeCards)
    await payments.confirm(trial, trial.cards.created[-1][0])
    messenger.texts.clear()
    clock.advance(days=2, hours=1)

    await mailer(on).run()

    assert mailed(messenger, "7") == [] and mailed(messenger, "8") == []


# --- Р6: повторный и параллельный проход ------------------------------------


async def test_a_repeated_and_a_parallel_pass_send_the_step_once(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)

    await asyncio.gather(mailer(on).run(), mailer(on).run())
    await mailer(on).run()

    assert mailed(messenger) == [R1_TEXT]
    assert await steps(storage, user) == [("R1", "sent")]


# --- Р7: заблокировал бота ---------------------------------------------------


async def test_a_blocked_bot_marks_the_person_and_the_send(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    logger: FakeLogger,
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    messenger.fail_send = RecipientGoneError("bot was blocked by the user")
    watched = replace(
        on,
        messenger=WatchedMessenger(
            messenger, storage=storage, logger=logger, now=clock
        ),
    )

    await mailer(watched).run()

    assert await steps(storage, user) == [("R1", "blocked")]
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.stopped_at == clock.now
    assert fresh.last_marketing_at is None


async def test_a_failed_send_keeps_only_the_error_class(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    messenger.fail_send = ConnectionError("сеть лежит, текст R1 тут ни при чём")

    await mailer(on).run()

    (row,) = await storage.mailings_of(user.id)
    assert (row.status, row.error_code) == (MailingStatus.ERROR, "ConnectionError")


# --- Р9: ссылка в тексте R3 --------------------------------------------------


async def test_r3_carries_the_personal_link_in_its_text(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    url = referral_url(
        on.settings.referral_link_host, on.settings.bot_username, user.referral_code
    )
    clock.advance(days=22, hours=1)

    await mailer(on).run()

    assert mailed(messenger) == [
        "Если у друга тоже горит доклад — просто перешли ему это сообщение.\n"
        "Ему презентация в подарок, тебе тоже 🎁\n"
        "\n"
        f"👉 {url}"
    ]


# --- Р10: нажатие кнопки ------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "label"), [(4, "📊 Написать тему"), (11, "Сделать презентацию")]
)
async def test_a_mailing_button_marks_the_press_and_asks_for_a_topic(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    days: int,
    label: str,
) -> None:
    user = await quiet(storage)
    clock.advance(days=days, hours=1)
    await mailer(on).run()
    sent = messenger.texts[-1]
    assert sent.keyboard is not None
    (button,) = [b for row in sent.keyboard.rows for b in row]
    assert button.text == label and button.action is not None
    await storage.add_bonus(user.id, presentations=1)
    clock.advance(minutes=30)

    await handle(
        on,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="7"),
            external_user_id="7",
            action=button.action,
        ),
    )

    (row,) = await storage.mailings_of(user.id)
    assert row.clicked_at == clock.now
    assert messenger.last_text.text == texts.presentation_ask().text


# --- Р11: выключатели и MAX -------------------------------------------------


@pytest.mark.parametrize(
    "switch",
    ["mailings_enabled", "mailing_r1_enabled"],
)
async def test_a_switched_off_mailing_sends_nothing(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    switch: str,
) -> None:
    await quiet(storage)
    off = replace(on, settings=replace(on.settings, **{switch: False}))  # type: ignore[arg-type]
    clock.advance(days=4, hours=1)

    await mailer(off).run()

    assert mailed(messenger) == []


async def test_max_never_gets_a_mailing(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="7",
        referral_code="max7",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    for _ in range(30 * 24):
        await mailer(on).run()
        clock.advance(hours=1)

    assert messenger.texts == []


# --- Р12: ни текста в логах, ни в таблице -----------------------------------


async def test_no_mailing_text_in_logs_or_the_table(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    logger: FakeLogger,
) -> None:
    user = await quiet(storage)
    for _ in range(30 * 24):
        await mailer(on).run()
        clock.advance(hours=1)

    stored = repr(await storage.mailings_of(user.id))
    logged = repr(logger.events)
    for fragment in ("Есть тема", "Дарим", "горит доклад", user.referral_code):
        assert fragment not in stored
        assert fragment not in logged
    assert "mailing_sent" in logged


# --- Правило 6: темп ------------------------------------------------------


async def test_every_send_waits_its_turn(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Каждое сообщение рассылки проходит через ограничитель темпа."""
    for n in range(5):
        await quiet(storage, str(10 + n))
    clock.advance(days=4, hours=1)
    turns: list[int] = []

    async def pace() -> None:
        turns.append(len(messenger.texts))

    await Mailer(deps=on, pace=pace).run()

    assert turns == [0, 1, 2, 3, 4]


async def test_all_quiet_people_get_their_step_across_batches(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    for n in range(5):
        await quiet(storage, str(10 + n))
    clock.advance(days=4, hours=1)

    await Mailer(deps=on, batch=2).run()

    assert len(messenger.texts) == 5


# --- Ядро перепроверяет то, что отобрала база -------------------------------


@pytest.mark.parametrize(
    "case", ["paid", "stopped", "fresh", "marketed", "max", "claimed"]
)
async def test_the_core_rechecks_every_candidate(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    case: str,
) -> None:
    """База могла отдать устаревший список: человек оплатил, остановил бота
    или написал, пока шёл проход. Ядро проверяет каждого заново."""
    kind = MessengerKind.MAX if case == "max" else MessengerKind.TELEGRAM
    user = await storage.create_user(
        messenger=kind,
        external_id="7",
        referral_code="code7",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    clock.advance(days=4, hours=1)
    if case == "paid":
        await storage.set_tariff(user.id, TariffId.LITE, clock.now + timedelta(days=1))
    if case == "stopped":
        await storage.mark_stopped(user.id, clock.now)
    if case == "fresh":
        await storage.touch_active(user.id, clock.now - timedelta(days=1))
    if case == "marketed":
        await storage.claim_mailing(user.id, "R3", clock.now, clock.now)
        await storage.finish_mailing(
            user.id, "R3", clock.now, status=MailingStatus.SENT, at=clock.now
        )
    if case == "claimed":
        assert user.last_active_at is not None
        await storage.claim_mailing(user.id, "R1", user.last_active_at, clock.now)

    calls: list[object] = []

    async def everyone(**_: object) -> list[User]:
        calls.append(_)
        assert len(calls) < 10, "проход ходит по кругу"
        found = await storage.get_user_by_id(user.id)
        assert found is not None
        return [found]

    storage.mailing_candidates = everyone  # type: ignore[method-assign]

    # Пачка по одному: отсеянный ядром не должен зациклить проход.
    await Mailer(deps=on, batch=1).run()

    assert messenger.texts == []


# --- Обещание «напиши тему одним сообщением» --------------------------------


@pytest.mark.parametrize("days", [4, 11])
async def test_after_r1_or_r2_the_next_message_is_the_topic(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    days: int,
) -> None:
    user = await quiet(storage)
    clock.advance(days=days, hours=1)
    await mailer(on).run()
    await storage.add_bonus(user.id, presentations=1)

    await handle(
        on,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="7"),
            external_user_id="7",
            text="Фотосинтез",
        ),
    )

    assert messenger.last_text.keyboard is not None
    labels = [b.text for row in messenger.last_text.keyboard.rows for b in row]
    assert texts.BUTTON_DECK_BUILD in labels


async def test_r3_does_not_wait_for_a_topic(
    on: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=22, hours=1)

    await mailer(on).run()

    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.pending is None


async def test_a_mailing_does_not_break_what_the_person_was_doing(
    on: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    """Человек замер посреди сценария — рассылка его ожидание не подменяет."""
    user = await quiet(storage)
    await storage.set_pending(user.id, "await:image")
    clock.advance(days=4, hours=1)

    await mailer(on).run()

    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.pending == "await:image"


async def test_without_presentations_nothing_is_promised(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Все три текста — про презентации: нет раздела — нет рассылки."""
    await quiet(storage)
    clock.advance(days=4, hours=1)

    await mailer(replace(on, presentations=None)).run()

    assert messenger.texts == []


# --- Сессия 13, Д1: ожидание темы после рассылки — не дольше суток ----------


def chat_message(**fields: object) -> IncomingMessage:
    return IncomingMessage(
        chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="7"),
        external_user_id="7",
        **fields,  # type: ignore[arg-type]
    )


def asked_for_parameters(messenger: FakeMessenger) -> bool:
    keyboard = messenger.last_text.keyboard
    labels = [b.text for row in keyboard.rows for b in row] if keyboard else []
    return texts.BUTTON_DECK_BUILD in labels


@pytest.mark.parametrize("days", [4, 11], ids=["R1", "R2"])
async def test_a_message_a_day_after_the_mailing_goes_to_the_chat(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    llm: FakeLLM,
    days: int,
) -> None:
    """Д1: прошли сутки — сообщение уже не тема, а вопрос в чат."""
    user = await quiet(storage)
    clock.advance(days=days, hours=1)
    await mailer(on).run()
    await storage.add_bonus(user.id, presentations=1)
    clock.advance(hours=24)

    await handle(on, chat_message(text="Почему небо голубое?"))

    assert len(llm.calls) == 1
    assert not asked_for_parameters(messenger)
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.pending is None


@pytest.mark.parametrize("days", [4, 11], ids=["R1", "R2"])
async def test_a_message_within_the_day_is_the_topic(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    llm: FakeLLM,
    days: int,
) -> None:
    """Д1: в пределах суток — тема презентации."""
    user = await quiet(storage)
    clock.advance(days=days, hours=1)
    await mailer(on).run()
    await storage.add_bonus(user.id, presentations=1)
    clock.advance(hours=23, minutes=59)

    await handle(on, chat_message(text="Фотосинтез"))

    assert llm.calls == []
    assert asked_for_parameters(messenger)


async def test_the_topic_wait_follows_the_setting(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    llm: FakeLLM,
) -> None:
    short = replace(on, settings=replace(on.settings, mailing_topic_wait_hours=2))
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await mailer(short).run()
    await storage.add_bonus(user.id, presentations=1)
    clock.advance(hours=2)

    await handle(short, chat_message(text="Почему небо голубое?"))

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"action": Action.MENU_PROFILE},
        {"action": Action.INVITE_FRIEND},
        {"start_payload": ""},
    ],
    ids=["menu", "button", "command"],
)
async def test_any_other_action_drops_the_topic_wait(
    on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    llm: FakeLLM,
    fields: dict[str, object],
) -> None:
    """Д1: кнопка меню или команда — ожидание темы снято, дальше обычный чат."""
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await mailer(on).run()
    await storage.add_bonus(user.id, presentations=1)
    clock.advance(minutes=5)

    await handle(on, chat_message(**fields))
    await handle(on, chat_message(text="Почему небо голубое?"))

    assert len(llm.calls) == 1
    assert not asked_for_parameters(messenger)


def test_the_mailing_wait_keeps_its_deadline() -> None:
    from datetime import UTC, datetime

    from app.core import pending

    until = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    state = pending.await_mailing_topic(until)

    assert pending.mailing_topic_until(state) == until
    assert pending.is_awaiting_presentation_topic(state)
    assert pending.mailing_topic_until(pending.AWAIT_PRESENTATION_TOPIC) is None
    assert pending.mailing_topic_until("await:pres:mail:вчера") is None
    assert pending.mailing_topic_until("await:pres:mail:2026-10-17T12:00:00") is None
    with pytest.raises(ValueError):
        pending.await_mailing_topic(until.replace(tzinfo=None))


async def test_a_photo_drops_the_topic_wait(
    on: Deps, storage: InMemoryStorage, clock: FrozenClock
) -> None:
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await mailer(on).run()

    await handle(on, chat_message(photo_ref="photo-1"))

    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None and fresh.pending is None


async def test_a_file_within_the_day_is_the_material(
    on: Deps, storage: InMemoryStorage, messenger: FakeMessenger, clock: FrozenClock
) -> None:
    """Файл в пределах суток — материал презентации, как после кнопки меню."""
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await mailer(on).run()
    await storage.add_bonus(user.id, presentations=1)

    await handle(on, chat_message(document_ref="file-1", document_name="конспект.txt"))

    # Файл ушёл в проверку материала, а не в «такое я пока не умею».
    assert len(messenger.downloads_limited_to) == 1
    assert messenger.last_text.text != texts.unsupported_input().text
