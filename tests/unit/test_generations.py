"""Учёт обращений к провайдерам — таблица generations.

Три вещи, ради которых он и заводился: пишется каждая попытка, включая
упавшую; в строке нет содержимого запроса; сбой самой записи не мешает
человеку получить результат, за который провайдеру уже заплачено.
"""

from __future__ import annotations

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core.generations import Generation, GenerationKind, GenerationStatus
from app.core.models import Photo
from app.core.scenarios import chat, images, presets
from app.core.scenarios.deps import Deps, Session
from config.presets import PRESETS
from tests.fakes import (
    PNG_BYTES,
    FakeImages,
    FakeLLM,
    FakeLogger,
    FakeMessenger,
    FrozenClock,
)

PHOTO = Photo(data=PNG_BYTES)


class BoomError(Exception):
    """Провайдер лёг."""


def only(storage: InMemoryStorage) -> Generation:
    assert len(storage.generations) == 1, storage.generations
    return storage.generations[0]


# --- Чат -----------------------------------------------------------------


async def test_a_chat_answer_is_recorded_with_its_tokens(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    await chat.handle_message(deps, session, "привет")

    written = only(storage)
    assert written.kind is GenerationKind.CHAT
    assert written.status is GenerationStatus.SUCCESS
    assert (written.tokens_in, written.tokens_out) == (11, 22)
    assert written.error_code is None
    assert written.preset_id is None


async def test_a_failed_chat_call_is_recorded_too(
    deps: Deps, session: Session, storage: InMemoryStorage, llm: FakeLLM
) -> None:
    """Провайдер берёт деньги за попытку, а не за успех.

    Без строк с отказами доля брака видна только по счёту в конце месяца.
    """
    llm.error = BoomError("провайдер лёг")

    await chat.handle_message(deps, session, "привет")

    written = only(storage)
    assert written.status is GenerationStatus.FAILED
    assert written.error_code == "BoomError"


async def test_the_record_never_carries_the_message_text(
    deps: Deps, session: Session, storage: InMemoryStorage, llm: FakeLLM
) -> None:
    """§3.5: содержимого сообщений мы не храним — ни в логах, ни здесь."""
    secret = "мой домашний адрес такой-то"
    llm.error = BoomError(secret)

    await chat.handle_message(deps, session, secret)

    assert secret not in repr(only(storage))


async def test_the_measured_duration_is_the_call_itself(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    llm: FakeLLM,
    clock: FrozenClock,
) -> None:
    llm.clock = clock
    llm.takes_seconds = 1.5

    await chat.handle_message(deps, session, "привет")

    assert only(storage).duration_ms == 1500


async def test_continuing_an_answer_is_a_separate_call(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """«Продолжить» — полноценный запрос к провайдеру, и стоит он денег."""
    await chat.handle_message(deps, session, "привет")
    await chat.continue_answer(deps, session)

    assert len(storage.generations) == 2
    assert all(each.kind is GenerationKind.CHAT for each in storage.generations)


# --- Картинки и приколы --------------------------------------------------


async def test_a_drawing_is_recorded_without_tokens(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """У картинок провайдер токены не называет, и нули были бы враньём."""
    await images.draw(deps, session, "кот-космонавт")

    written = only(storage)
    assert written.kind is GenerationKind.IMAGE
    assert written.status is GenerationStatus.SUCCESS
    assert (written.tokens_in, written.tokens_out) == (None, None)


async def test_a_failed_drawing_is_recorded(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    images_.error = BoomError("провайдер лёг")

    await images.draw(deps, session, "кот-космонавт")

    assert only(storage).status is GenerationStatus.FAILED


async def test_a_preset_names_itself_in_the_record(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Иначе по таблице не сказать, какой прикол чаще всего отказывает."""
    await presets.apply(deps, session, PRESETS["lego"], [PHOTO])

    written = only(storage)
    assert written.kind is GenerationKind.PRESET
    assert written.preset_id == "lego"


async def test_a_failed_preset_is_recorded_with_its_preset(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    images_.error = BoomError("провайдер лёг")

    await presets.apply(deps, session, PRESETS["lego"], [PHOTO])

    written = only(storage)
    assert (written.status, written.preset_id) == (GenerationStatus.FAILED, "lego")


async def test_a_photo_rejected_before_the_provider_costs_no_record(
    deps: Deps, session: Session, storage: InMemoryStorage
) -> None:
    """Записываем обращения к провайдеру, а этого обращения не было."""
    await presets.apply(deps, session, PRESETS["lego"], [Photo(data=b"not a picture")])

    assert storage.generations == []


# --- Учёт не ломает продукт ----------------------------------------------


class BrokenGenerations(InMemoryStorage):
    """Хранилище, у которого падает только запись учёта."""

    async def record_generation(self, generation: Generation) -> None:
        raise RuntimeError("таблица недоступна")


@pytest.fixture
def broken_storage(clock: FrozenClock) -> BrokenGenerations:
    return BrokenGenerations(clock=clock)


async def test_a_broken_record_does_not_break_the_answer(
    deps: Deps,
    session: Session,
    broken_storage: BrokenGenerations,
    messenger: FakeMessenger,
    logger: FakeLogger,
) -> None:
    """Провайдеру уже заплачено — человек обязан получить свой ответ.

    Учёт стоит сбоку от продукта: его сбой уходит в лог и на этом кончается.
    """
    from dataclasses import replace as replace_deps

    user = await broken_storage.create_user(
        messenger=session.user.messenger,
        external_id=session.user.external_id,
        referral_code="code-broken",
        support_number=424242,
        bonus_images=3,
    )
    with_broken = replace_deps(deps, storage=broken_storage)
    broken_session = replace_deps(session, user=user)

    await chat.handle_message(with_broken, broken_session, "привет")

    assert messenger.texts_said() == ["Ответ."]
    assert "generation_not_recorded" in logger.names()
