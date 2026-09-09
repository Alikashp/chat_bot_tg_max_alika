"""Разовый бонус за подписку на канал.

Третья ступень бесплатной лестницы: три картинки при регистрации, по две за
друга и две — один раз — за подписку на канал. Дальше тарифы.

Главное здесь — что бонус нельзя получить дважды и нельзя получить, не
подписавшись: и то и другое превратило бы бесплатные картинки в кнопку,
которую жмут сколько нужно.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from app.adapters.storage.memory import InMemoryStorage
from app.adapters.telegram.channel import TelegramChannel
from app.core import texts
from app.core.channel import channel_username
from app.core.limits import LimitKind
from app.core.models import Button, User
from app.core.scenarios import channel, paywall
from app.core.scenarios.deps import Deps, Session
from tests.fakes import FakeChannel, FakeLogger, FakeMessenger

CHANNEL = "https://t.me/chatgptbotonline"


@pytest.fixture
def deps_with_channel(deps: Deps) -> Deps:
    """Зависимости, в которых канал настроен, — как в Telegram."""
    return replace(deps, settings=replace(deps.settings, channel_url=CHANNEL))


async def refresh(storage: InMemoryStorage, user: User) -> User:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


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


# --- Предложение ---------------------------------------------------------


async def test_the_offer_names_the_reward_and_leads_to_the_channel(
    deps_with_channel: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await channel.show_offer(deps_with_channel, session)

    assert "+2 картинки" in messenger.last_text.text
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert keyboard.rows[0][0].url == CHANNEL


async def test_without_a_configured_channel_nothing_is_offered(
    deps: Deps, session: Session
) -> None:
    """Пустая ссылка — это «предложения нет», а не «ссылка потерялась»."""
    assert channel.available(deps, session) == 0


async def test_in_a_messenger_without_a_channel_nothing_is_offered(
    deps_with_channel: Deps, session: Session
) -> None:
    """В MAX проверять подписку нечем — значит и обещать её нечего.

    Показать кнопку и не суметь проверить было бы хуже, чем не показывать:
    человек подписался бы и не получил обещанного.
    """
    without = replace(deps_with_channel, channel=None)

    assert channel.available(without, session) == 0


async def test_the_bonus_is_not_offered_twice(
    deps_with_channel: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    assert await storage.grant_channel_bonus(user.id, images=2)
    taken = replace(session, user=await refresh(storage, user))

    assert channel.available(deps_with_channel, taken) == 0


# --- Проверка подписки ---------------------------------------------------


async def test_a_subscriber_gets_the_images(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    channel_: FakeChannel,
    user: User,
) -> None:
    channel_.members.add(user.external_id)

    await channel.check(deps_with_channel, session)

    # Три с регистрации плюс две за канал.
    assert (await refresh(storage, user)).bonus_images == 5
    assert "+2 картинки" in messenger.last_text.text


async def test_the_bonus_is_granted_only_once(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    channel_: FakeChannel,
    user: User,
) -> None:
    """Подписаться, забрать, отписаться, подписаться снова — так не выйдет."""
    channel_.members.add(user.external_id)

    await channel.check(deps_with_channel, session)
    await channel.check(deps_with_channel, session)

    assert (await refresh(storage, user)).bonus_images == 5


async def test_a_non_subscriber_gets_nothing_but_is_told_how(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    user: User,
) -> None:
    await channel.check(deps_with_channel, session)

    assert (await refresh(storage, user)).bonus_images == 3
    assert messenger.last_text.text == texts.channel_not_subscribed().text
    # Тупика быть не должно: с экрана видно, куда идти подписываться.
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert keyboard.rows[0][0].url == CHANNEL


async def test_a_failed_check_is_not_the_same_as_a_refusal(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    channel_: FakeChannel,
    logger: FakeLogger,
    user: User,
) -> None:
    """«Не смогли проверить» — не повод сказать человеку «ты не подписан».

    Свалив одно в другое, мы не выдали бы заслуженный бонус и оставили бы
    человека думать, что его обманули.
    """
    channel_.members.add(user.external_id)
    channel_.error = RuntimeError("телеграм не ответил")

    await channel.check(deps_with_channel, session)

    assert (await refresh(storage, user)).bonus_images == 3
    assert messenger.last_text.text == texts.channel_check_failed().text
    assert "channel_check_failed" in logger.names()


async def test_a_second_claim_is_answered_without_a_dead_end(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    channel_: FakeChannel,
    user: User,
) -> None:
    channel_.members.add(user.external_id)
    assert await storage.grant_channel_bonus(user.id, images=2)

    await channel.check(deps_with_channel, session)

    assert messenger.last_text.text == texts.channel_already_taken(invite_images=2).text
    assert messenger.last_text.keyboard is not None


# --- Пейволл -------------------------------------------------------------


async def test_the_paywall_offers_the_channel_while_it_is_owed(
    deps_with_channel: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await paywall.show(deps_with_channel, session, LimitKind.IMAGES)

    labels = [button.text for row in _rows(messenger) for button in row]
    assert "📣 Канал → +2 картинки" in labels


async def test_the_paywall_hides_the_channel_once_it_is_taken(
    deps_with_channel: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    user: User,
) -> None:
    """Кнопка, ведущая на «уже получено», — это тупик наоборот."""
    assert await storage.grant_channel_bonus(user.id, images=2)
    taken = replace(session, user=await refresh(storage, user))

    await paywall.show(deps_with_channel, taken, LimitKind.IMAGES)

    labels = [button.text for row in _rows(messenger) for button in row]
    assert not any(label.startswith("📣") for label in labels)


async def test_the_paywall_screen_and_its_buttons_promise_the_same(
    deps_with_channel: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Экран объявляет линтеру одни кнопки, а человек видит другие — так нельзя."""
    await paywall.show(deps_with_channel, session, LimitKind.IMAGES)

    declared = texts.paywall_images(
        renews_tomorrow=False, invite_images=2, channel_images=2
    ).buttons
    shown = tuple(button.text for row in _rows(messenger) for button in row)
    assert declared == shown


def _rows(messenger: FakeMessenger) -> tuple[tuple[Button, ...], ...]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return keyboard.rows


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
