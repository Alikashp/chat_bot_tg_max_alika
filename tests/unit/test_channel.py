"""Обязательная подписка на канал (сессия 8, Г1–Г6).

Включается настройкой, по умолчанию выключена; только Telegram. Бесплатный
неподписанный человек не получает ни одного платного для нас результата:
чат, картинки, приколы, доклады и презентации закрыты экраном подписки.
Приветствие, профиль, тарифы, оплата и отключение продления открыты всегда.
Платящим (и в пробном периоде) проверки нет. Положительный ответ Telegram
запоминается на десять минут, отрицательный — нет; не дал проверить —
человека пропускаем. Бонус +2 картинки за канал убран.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.telegram.channel import TelegramChannel
from app.core import pending, texts
from app.core.actions import Action, buy_action, preset_action
from app.core.channel import channel_username
from app.core.limits import LimitKind
from app.core.models import (
    Chat,
    IncomingMessage,
    MessengerKind,
    TariffId,
    User,
)
from app.core.router import handle
from app.core.scenarios import keyboards, paywall, spending
from app.core.scenarios.deps import Deps, session_for
from tests.fakes import (
    FakeChannel,
    FakeImages,
    FakeLLM,
    FakeLogger,
    FakeMessenger,
    FrozenClock,
)

CHANNEL = "https://t.me/chatgptbotonline"
CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1", person="1")


@pytest.fixture
def gated(deps: Deps) -> Deps:
    """Канал настроен и подписка обязательна — как в Telegram с настройкой."""
    return replace(
        deps,
        settings=replace(deps.settings, channel_url=CHANNEL, channel_required=True),
    )


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


def labels(messenger: FakeMessenger) -> list[tuple[str, str | None, str | None]]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [(b.text, b.action, b.url) for row in keyboard.rows for b in row]


async def refresh(storage: InMemoryStorage, user: User) -> User:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


async def paid(storage: InMemoryStorage, user: User, clock: FrozenClock) -> None:
    await storage.set_tariff(
        user.id, TariffId.LITE, expires_at=clock.now + timedelta(days=3)
    )


# --- Разбор ссылки -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://t.me/chatgptbotonline", "@chatgptbotonline"),
        ("https://t.me/chatgptbotonline/", "@chatgptbotonline"),
        ("t.me/chatgptbotonline", "@chatgptbotonline"),
        # Приглашение в приватный канал: публичного имени у него нет, и
        # проверять подписку не по чему.
        ("https://t.me/+AbCdEf", ""),
        ("https://vk.com/chatgptbotonline", ""),
        ("", ""),
    ],
)
def test_channel_name_comes_from_the_link(url: str, expected: str) -> None:
    """Отдельной переменной с именем канала нет: ей не с чем расходиться."""
    assert channel_username(url) == expected


# --- Г1: неподписанный бесплатный — ничего платного ----------------------

#: Всё, что стоит нам денег: чат, картинки, приколы, доклады, презентации.
PAID_WORK = [
    {"text": "привет"},
    {"action": Action.MENU_IMAGES},
    {"action": Action.MENU_PRESETS},
    {"action": preset_action("lego")},
    {"photo_ref": "photo"},
    {"action": Action.MENU_DOCUMENTS},
    {"document_ref": "file", "document_name": "a.docx"},
    {"action": Action.MENU_PRESENTATIONS},
    {"action": Action.IMAGE_AGAIN},
    {"action": Action.CHAT_CONTINUE},
    {"text": texts.MENU_IMAGES},
]


@pytest.mark.parametrize("fields", PAID_WORK)
async def test_an_unsubscribed_free_user_meets_the_channel_screen(
    gated: Deps,
    user: User,
    messenger: FakeMessenger,
    llm: FakeLLM,
    images_: FakeImages,
    channel_: FakeChannel,
    fields: dict[str, object],
) -> None:
    before = await spending.current_allowance(
        gated, session_for(gated, user), LimitKind.MESSAGES
    )

    await handle(gated, incoming(**fields))

    assert messenger.last_text.text == texts.CHANNEL_REQUIRED
    assert labels(messenger) == [
        (texts.BUTTON_OPEN_CHANNEL, None, CHANNEL),
        (texts.BUTTON_CHANNEL_CHECK, Action.CHANNEL_CHECK, None),
    ]
    assert llm.calls == [] and images_.generated == [] and images_.edited == []
    after = await spending.current_allowance(
        gated, session_for(gated, user), LimitKind.MESSAGES
    )
    assert after == before
    assert channel_.asked == ["1"]


async def test_after_subscribing_the_person_goes_on_without_start(
    gated: Deps,
    user: User,
    messenger: FakeMessenger,
    llm: FakeLLM,
    channel_: FakeChannel,
) -> None:
    await handle(gated, incoming(text="привет"))
    channel_.members.add("1")

    await handle(gated, incoming(action=Action.CHANNEL_CHECK))
    assert messenger.last_text.text == texts.CHANNEL_PASSED

    llm.answer = "Привет!"
    await handle(gated, incoming(text="привет"))
    assert messenger.last_text.text == "Привет!"


async def test_a_check_without_a_subscription_says_how(
    gated: Deps, user: User, messenger: FakeMessenger
) -> None:
    await handle(gated, incoming(action=Action.CHANNEL_CHECK))

    assert messenger.last_text.text == texts.channel_not_subscribed().text
    assert [label for label, _, _ in labels(messenger)] == [
        texts.BUTTON_OPEN_CHANNEL,
        texts.BUTTON_CHANNEL_CHECK,
    ]


# --- Г2: платящий, MAX, выключенная настройка — проверки нет -------------


async def test_a_paying_person_is_never_asked(
    gated: Deps,
    user: User,
    storage: InMemoryStorage,
    clock: FrozenClock,
    llm: FakeLLM,
    messenger: FakeMessenger,
    channel_: FakeChannel,
) -> None:
    """Пробный период — тот же Лайт со сроком: и его проверка не касается."""
    await paid(storage, user, clock)
    llm.answer = "Ответ."

    await handle(gated, incoming(text="привет"))

    assert messenger.last_text.text == "Ответ."
    assert channel_.asked == []


async def test_a_max_user_is_never_asked(
    gated: Deps,
    storage: InMemoryStorage,
    llm: FakeLLM,
    messenger: FakeMessenger,
    channel_: FakeChannel,
) -> None:
    from app.core import support

    await storage.create_user(
        messenger=MessengerKind.MAX,
        external_id="77",
        referral_code="maxuser",
        support_number=support.generate_number(),
        bonus_images=3,
        bonus_documents=0,
    )
    llm.answer = "Ответ."

    await handle(
        gated,
        IncomingMessage(
            chat=Chat(messenger=MessengerKind.MAX, chat_id="5", person="77"),
            external_user_id="77",
            text="привет",
        ),
    )

    assert messenger.last_text.text == "Ответ."
    assert channel_.asked == []


async def test_without_the_setting_nobody_is_asked(
    deps: Deps, user: User, llm: FakeLLM, channel_: FakeChannel
) -> None:
    on_channel = replace(deps, settings=replace(deps.settings, channel_url=CHANNEL))

    await handle(on_channel, incoming(text="привет"))

    assert llm.calls and channel_.asked == []


# --- Г3: профиль, тарифы, оплата, отключение продления -------------------


@pytest.mark.parametrize(
    "action",
    [
        Action.MENU_PROFILE,
        Action.MENU_TARIFFS,
        Action.OPEN_TARIFFS,
        Action.SUBSCRIPTION,
        Action.SUBSCRIPTION_OFF,
        buy_action(TariffId.LITE.value),
    ],
)
async def test_the_money_side_stays_open(
    gated: Deps,
    user: User,
    messenger: FakeMessenger,
    channel_: FakeChannel,
    action: str,
) -> None:
    await handle(gated, incoming(action=action))

    assert texts.CHANNEL_REQUIRED not in messenger.texts_said()
    assert channel_.asked == []


async def test_an_email_for_the_receipt_is_not_stopped(
    gated: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    channel_: FakeChannel,
) -> None:
    """Почта для чека — середина оплаты, а не вопрос в чат."""
    await storage.set_pending(user.id, pending.await_email(TariffId.LITE.value))

    await handle(gated, incoming(text="me@example.com"))

    assert texts.CHANNEL_REQUIRED not in messenger.texts_said()
    assert channel_.asked == []


async def test_the_greeting_is_open(
    gated: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    channel_: FakeChannel,
) -> None:
    await handle(gated, incoming(start_payload=""))

    assert messenger.texts and texts.CHANNEL_REQUIRED not in messenger.texts_said()
    assert channel_.asked == []


# --- Г4: успешная проверка помнится десять минут -----------------------


async def test_a_positive_answer_is_remembered_for_ten_minutes(
    gated: Deps,
    user: User,
    clock: FrozenClock,
    messenger: FakeMessenger,
    channel_: FakeChannel,
) -> None:
    channel_.members.add("1")
    for _ in range(3):
        await handle(gated, incoming(text="привет"))
    assert channel_.asked == ["1"]

    # Отписался — через десять минут проверка снова спросит и остановит.
    channel_.members.clear()
    clock.advance(minutes=9)
    await handle(gated, incoming(text="привет"))
    assert channel_.asked == ["1"]
    clock.advance(minutes=1, seconds=1)
    await handle(gated, incoming(text="привет"))
    assert channel_.asked == ["1", "1"]
    assert messenger.last_text.text == texts.CHANNEL_REQUIRED


async def test_the_memory_is_a_setting(
    gated: Deps, user: User, clock: FrozenClock, channel_: FakeChannel
) -> None:
    short = replace(
        gated,
        settings=replace(gated.settings, channel_check_ttl=timedelta(minutes=1)),
    )
    channel_.members.add("1")
    await handle(short, incoming(text="привет"))
    clock.advance(minutes=2)

    await handle(short, incoming(text="привет"))

    assert channel_.asked == ["1", "1"]


async def test_a_negative_answer_is_not_remembered(
    gated: Deps, user: User, channel_: FakeChannel
) -> None:
    await handle(gated, incoming(text="привет"))
    channel_.members.add("1")

    await handle(gated, incoming(text="привет"))

    assert channel_.asked == ["1", "1"]


async def test_a_successful_check_button_is_remembered_too(
    gated: Deps, user: User, channel_: FakeChannel
) -> None:
    channel_.members.add("1")
    await handle(gated, incoming(action=Action.CHANNEL_CHECK))

    await handle(gated, incoming(text="привет"))

    assert channel_.asked == ["1"]


# --- Г5: сбой проверки пропускает человека -----------------------------


async def test_a_failed_check_lets_the_person_through(
    gated: Deps,
    user: User,
    llm: FakeLLM,
    messenger: FakeMessenger,
    channel_: FakeChannel,
    logger: FakeLogger,
) -> None:
    channel_.error = RuntimeError("member list is inaccessible")
    llm.answer = "Ответ."

    await handle(gated, incoming(text="привет"))

    assert messenger.last_text.text == "Ответ."
    assert "channel_gate_skipped" in logger.names()


async def test_a_failed_check_button_lets_the_person_through(
    gated: Deps, user: User, messenger: FakeMessenger, channel_: FakeChannel
) -> None:
    channel_.error = RuntimeError("сеть")

    await handle(gated, incoming(action=Action.CHANNEL_CHECK))

    assert messenger.last_text.text == texts.CHANNEL_PASSED


# --- Г6: бонус за канал больше не обещается ----------------------------


async def test_the_paywall_offers_no_channel_bonus(
    gated: Deps,
    user: User,
    messenger: FakeMessenger,
) -> None:
    await paywall.show(gated, session_for(gated, user), LimitKind.IMAGES)

    assert all(action != Action.CHANNEL_OFFER for _, action, _ in labels(messenger))
    assert "Канал" not in messenger.last_text.text


async def test_an_old_bonus_button_grants_nothing(
    gated: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Кнопка «📣 Канал → +2 картинки» из старой переписки ведёт в меню."""
    before = (await refresh(storage, user)).bonus_images

    await handle(gated, incoming(action=Action.CHANNEL_OFFER))

    assert (await refresh(storage, user)).bonus_images == before
    assert messenger.last_text.text == texts.menu(keyboards.menu_labels()).text


def test_no_screen_promises_images_for_the_channel() -> None:
    for screen in texts.SCREENS:
        assert "Канал →" not in " ".join(screen.buttons)
        assert "за подписку" not in screen.text


# --- Адаптер Telegram ----------------------------------------------------


class _Bot:
    """Бот, отвечающий заранее заданным статусом или отказом."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked: list[tuple[str, int]] = []

    async def get_chat_member(self, *, chat_id: str, user_id: int) -> object:
        self.asked.append((chat_id, user_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Member:
    def __init__(self, status: str, *, is_member: bool = False) -> None:
        self.status = status
        self.is_member = is_member


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ChatMemberStatus.CREATOR, True),
        (ChatMemberStatus.ADMINISTRATOR, True),
        (ChatMemberStatus.MEMBER, True),
        (ChatMemberStatus.LEFT, False),
        (ChatMemberStatus.KICKED, False),
    ],
)
async def test_the_adapter_reads_the_status(
    status: ChatMemberStatus, expected: bool, logger: FakeLogger
) -> None:
    adapter = TelegramChannel(_Bot(_Member(status)), "@chan", logger)  # type: ignore[arg-type]

    assert await adapter.has_member("42") is expected


@pytest.mark.parametrize("is_member", [True, False])
async def test_a_restricted_member_is_judged_by_membership(
    is_member: bool, logger: FakeLogger
) -> None:
    """У ограниченного участника статус один, а в канале он или нет — поле."""
    member = _Member(ChatMemberStatus.RESTRICTED, is_member=is_member)
    adapter = TelegramChannel(_Bot(member), "@chan", logger)  # type: ignore[arg-type]

    assert await adapter.has_member("42") is is_member


async def test_a_missing_user_is_simply_not_subscribed(logger: FakeLogger) -> None:
    refusal = TelegramBadRequest(
        method=GetChatMember(chat_id="@chan", user_id=42),
        message="Bad Request: user not found",
    )
    adapter = TelegramChannel(_Bot(refusal), "@chan", logger)  # type: ignore[arg-type]

    assert await adapter.has_member("42") is False


async def test_a_bot_without_rights_does_not_pass_for_a_refusal(
    logger: FakeLogger,
) -> None:
    """Бот не админ канала — «проверить не смогли», а не «ты не подписан».

    Свалив одно в другое, бот сообщал бы подписавшимся, что подписки не
    видит, и делал бы это тем убедительнее, чем хуже настроен канал.
    """
    refusal = TelegramBadRequest(
        method=GetChatMember(chat_id="@chan", user_id=42),
        message="Bad Request: member list is inaccessible",
    )
    adapter = TelegramChannel(_Bot(refusal), "@chan", logger)  # type: ignore[arg-type]

    with pytest.raises(TelegramBadRequest):
        await adapter.has_member("42")

    assert "channel_check_refused" in logger.names()
