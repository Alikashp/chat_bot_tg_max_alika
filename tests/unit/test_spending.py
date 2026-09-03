"""Главный инвариант проекта — критерий приёмки №4.

    Лимит не списывается за упавший запрос. Списание происходит только по
    факту успешно доставленного пользователю результата.

Инвариант проверяется здесь по всем трём сценариям, которые тратят лимиты, и
по обоим способам провалиться: когда падает провайдер и когда падает сама
доставка. Второй случай тоньше первого и без теста забывается: провайдер уже
ответил, деньги за него уплачены, и рука тянется списать лимит, хотя человек
результата не увидел.
"""

from __future__ import annotations

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core.limits import LimitKind
from app.core.models import Photo, User
from app.core.scenarios import chat, images, presets, spending
from app.core.scenarios.deps import Deps, Session
from config.presets import PRESETS
from tests.fakes import PNG_BYTES, FakeImages, FakeLLM, FakeLogger, FakeMessenger


class BoomError(Exception):
    """Провайдер лёг."""


class UndeliveredError(Exception):
    """Ответ есть, а доставить не вышло."""


async def used(storage: InMemoryStorage, session: Session) -> tuple[int, int]:
    usage = await storage.get_usage(session.user.id, session.day)
    return usage.messages_used, usage.images_used


async def bonus(storage: InMemoryStorage, user: User) -> tuple[int, int]:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh.bonus_messages, fresh.bonus_images


# --- Чат -----------------------------------------------------------------


async def test_chat_charges_after_a_delivered_answer(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    await chat.handle_message(deps, session, "привет")

    assert await used(storage, session) == (1, 0)


async def test_chat_does_not_charge_when_the_provider_fails(
    deps: Deps, session: Session, storage: InMemoryStorage, llm: FakeLLM
) -> None:
    llm.error = BoomError("провайдер лёг")

    await chat.handle_message(deps, session, "привет")

    assert await used(storage, session) == (0, 0)


async def test_chat_shows_an_error_with_a_retry_button(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    """§2.2: пользователю обещано, что сообщение не потратилось."""
    llm.error = BoomError("провайдер лёг")

    await chat.handle_message(deps, session, "привет")

    assert "Сообщение не потратилось" in messenger.last_text.text
    assert messenger.last_text.keyboard is not None


async def test_chat_does_not_charge_when_delivery_fails(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
) -> None:
    """Провайдер ответил, но человек ответа не увидел — платить не должен."""
    messenger.fail_send = UndeliveredError("сеть отвалилась")

    with pytest.raises(UndeliveredError):
        await chat.handle_message(deps, session, "привет")

    assert await used(storage, session) == (0, 0)


async def test_failed_chat_does_not_pollute_the_dialog(
    deps: Deps, session: Session, storage: InMemoryStorage, llm: FakeLLM
) -> None:
    """Неудачный заход не должен оставлять после себя половину диалога."""
    llm.error = BoomError("провайдер лёг")

    await chat.handle_message(deps, session, "привет")

    assert (await storage.get_dialog(session.user.id)).turns == ()


# --- Картинки ------------------------------------------------------------


async def test_image_charges_after_delivery(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    await images.draw(deps, session, "кот-космонавт")

    assert await used(storage, session) == (0, 1)


async def test_image_does_not_charge_when_the_provider_fails(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    images_.error = BoomError("провайдер лёг")

    await images.draw(deps, session, "кот-космонавт")

    assert await used(storage, session) == (0, 0)


async def test_image_does_not_charge_when_delivery_fails(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
) -> None:
    messenger.fail_edit_to_photo = UndeliveredError("сеть отвалилась")

    with pytest.raises(UndeliveredError):
        await images.draw(deps, session, "кот-космонавт")

    assert await used(storage, session) == (0, 0)


# --- Пресеты -------------------------------------------------------------


async def test_preset_charges_after_delivery(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    await presets.apply(deps, session, PRESETS["lego"], [Photo(data=PNG_BYTES)])

    assert await used(storage, session) == (0, 1)


async def test_preset_does_not_charge_when_the_provider_fails(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    images_.error = BoomError("провайдер лёг")

    await presets.apply(deps, session, PRESETS["lego"], [Photo(data=PNG_BYTES)])

    assert await used(storage, session) == (0, 0)


async def test_rejected_photo_costs_nothing(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    """Отказ до обращения к провайдеру: ни лимита, ни запроса."""
    await presets.apply(
        deps, session, PRESETS["lego"], [Photo(data="это не картинка".encode())]
    )

    assert await used(storage, session) == (0, 0)
    assert images_.edited == []


# --- Порядок корзин ------------------------------------------------------


async def test_daily_quota_is_spent_before_the_bonus(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    await storage.add_bonus(user.id, messages=50, images=5)

    await spending.charge(deps, session, LimitKind.MESSAGES)

    assert await used(storage, session) == (1, 0)
    assert await bonus(storage, user) == (50, 5)


async def test_bonus_is_spent_once_the_daily_quota_is_gone(
    deps: Deps, session: Session, storage: InMemoryStorage, user: User
) -> None:
    await storage.add_bonus(user.id, messages=50, images=5)
    await storage.add_usage(user.id, session.day, messages=20)

    await spending.charge(deps, session, LimitKind.MESSAGES)

    assert await bonus(storage, user) == (49, 5)


async def test_charging_with_nothing_left_is_reported(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    user: User,
    logger: FakeLogger,
) -> None:
    """Такого быть не должно, но молчать о таком нельзя."""
    await storage.add_usage(user.id, session.day, messages=20)

    await spending.charge(deps, session, LimitKind.MESSAGES)

    assert "charged_over_limit" in logger.names()
