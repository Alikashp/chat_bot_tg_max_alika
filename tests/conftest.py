"""Общие фикстуры для тестов сценариев."""

from __future__ import annotations

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support
from app.core.models import Chat, MessengerKind, User
from app.core.scenarios.deps import Deps, Session
from app.core.settings import CoreSettings
from tests.fakes import (
    FakeCards,
    FakeGuard,
    FakeImages,
    FakeLLM,
    FakeLogger,
    FakeMessenger,
    FakeStars,
    FrozenClock,
)


@pytest.fixture
def storage(clock: FrozenClock) -> InMemoryStorage:
    """Хранилище живёт по тем же часам, что и сценарии."""
    return InMemoryStorage(clock=clock)


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def images_() -> FakeImages:
    """Провайдер картинок.

    Подчёркивание в имени — не небрежность: без него фикстура затеняла бы
    модуль сценария app.core.scenarios.images в тестах, которые нужны оба.
    """
    return FakeImages()


@pytest.fixture
def logger() -> FakeLogger:
    return FakeLogger()


@pytest.fixture
def guard() -> FakeGuard:
    """По умолчанию одна задача на ключ — как в бою (§3.4.8)."""
    return FakeGuard(limit=1)


@pytest.fixture
def cards() -> FakeCards:
    """Оплата картой настроена — как в Telegram и в MAX с ключами ЮKassa."""
    return FakeCards()


@pytest.fixture
def stars() -> FakeStars:
    """Оплата звёздами есть — как в Telegram. Для MAX тесты подставляют None."""
    return FakeStars()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def settings() -> CoreSettings:
    return CoreSettings(
        bot_username="testbot",
        # Оплата не показывается, пока документы не опубликованы, — как и в бою.
        offer_url="https://telegra.ph/offer",
        privacy_url="https://telegra.ph/privacy",
        docs_version="2026-08-31",
        bank_statement_name="YM*ChatAIBot",
    )


@pytest.fixture
def deps(
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    llm: FakeLLM,
    images_: FakeImages,
    settings: CoreSettings,
    logger: FakeLogger,
    guard: FakeGuard,
    cards: FakeCards,
    stars: FakeStars,
    clock: FrozenClock,
) -> Deps:
    return Deps(
        storage=storage,
        messenger=messenger,
        llm=llm,
        images=images_,
        settings=settings,
        logger=logger,
        guard=guard,
        cards=cards,
        stars=stars,
        now=clock,
    )


@pytest.fixture
async def user(storage: InMemoryStorage) -> User:
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        referral_code="code1",
        support_number=support.generate_number(),
        daily_image_quota=3,
    )


@pytest.fixture
def session(deps: Deps, user: User) -> Session:
    return Session(
        user=user,
        chat=Chat(messenger=MessengerKind.TELEGRAM, chat_id="1"),
        day=deps.today(),
        now=deps.now(),
    )
