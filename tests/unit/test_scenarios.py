"""Тесты продуктовых сценариев.

Сценарии живут в core и ничего не знают про мессенджер: в тестах им
подсовываются фейки портов. Тот же код на фазе 7 поедет в MAX без единой
правки — это и проверяется на приёмке (критерий A4).
"""

from __future__ import annotations

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import parse_preset_action
from app.core.models import (
    Photo,
)
from app.core.scenarios import (
    chat,
    images,
    presets,
    profile,
    referral,
    tariffs,
)
from app.core.scenarios.deps import Deps, Session
from config import presets as registry
from config.presets import PRESETS, Preset
from tests.fakes import PNG_BYTES, FakeImages, FakeLLM, FakeMessenger

PHOTO = Photo(data=PNG_BYTES)


# --- Чат (§2.2) ----------------------------------------------------------


async def test_chat_answers_the_user(
    deps: Deps, session: Session, llm: FakeLLM, messenger: FakeMessenger
) -> None:
    llm.answer = "Столица Франции — Париж."

    await chat.handle_message(deps, session, "какая столица Франции?")

    assert messenger.last_text.text == "Столица Франции — Париж."


async def test_chat_shows_typing_while_it_thinks(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§2.2: пока идёт ответ — статус «печатает…»."""
    await chat.handle_message(deps, session, "привет")

    assert messenger.typing == [session.chat]


async def test_chat_remembers_the_context(
    deps: Deps, session: Session, llm: FakeLLM
) -> None:
    """§2.2: контекст помнится всегда, переключателя нет."""
    await chat.handle_message(deps, session, "меня зовут Алиса")
    await chat.handle_message(deps, session, "как меня зовут?")

    last_turns, _ = llm.calls[-1]
    assert [turn.content for turn in last_turns] == [
        "меня зовут Алиса",
        "Ответ.",
        "как меня зовут?",
    ]


async def test_context_is_trimmed_to_the_configured_depth(
    deps: Deps, session: Session, llm: FakeLLM
) -> None:
    """Счёт провайдера растёт вместе с контекстом, а польза старых реплик — нет."""
    for index in range(15):
        await chat.handle_message(deps, session, f"сообщение {index}")

    last_turns, _ = llm.calls[-1]
    assert len(last_turns) <= deps.settings.dialog_max_turns


async def test_new_dialog_button_appears_exactly_on_the_tenth_message(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§2.2: ровно с десятого — под девятым кнопки ещё нет."""
    for _ in range(9):
        await chat.handle_message(deps, session, "вопрос")
    assert messenger.last_text.keyboard is None

    await chat.handle_message(deps, session, "вопрос")
    assert messenger.last_text.keyboard is not None
    assert messenger.last_text.keyboard.rows[0][0].text == texts.BUTTON_NEW_DIALOG


async def test_new_dialog_button_stays_after_the_tenth(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    for _ in range(12):
        await chat.handle_message(deps, session, "вопрос")

    assert messenger.last_text.keyboard is not None


async def test_new_dialog_forgets_the_context(
    deps: Deps, session: Session, storage: InMemoryStorage, llm: FakeLLM
) -> None:
    await chat.handle_message(deps, session, "меня зовут Алиса")

    await chat.start_new_dialog(deps, session)

    assert (await storage.get_dialog(session.user.id)).turns == ()


async def test_chat_shows_the_paywall_when_messages_run_out(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """§2.5: пейволл только при исчерпании."""
    await storage.add_usage(session.user.id, session.day, messages=20)

    await chat.handle_message(deps, session, "привет")

    assert "закончились" in messenger.last_text.text
    assert len(messenger.last_text.keyboard.rows) == 2  # type: ignore[union-attr]


async def test_paywall_is_not_shown_while_anything_is_left(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Никогда на входе — только когда обе корзины пусты."""
    await storage.add_usage(session.user.id, session.day, messages=19)

    await chat.handle_message(deps, session, "привет")

    assert "закончились" not in messenger.last_text.text


# --- Картинки (§2.3) -----------------------------------------------------


async def test_drawing_message_is_replaced_by_the_picture(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§2.3: «Рисую…» заменяется картинкой, а не дополняется ею."""
    await images.draw(deps, session, "кот-космонавт")

    assert messenger.texts_said() == [texts.IMAGE_DRAWING]
    assert len(messenger.photo_edits) == 1
    assert messenger.photos == []


async def test_picture_comes_with_its_buttons(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await images.draw(deps, session, "кот-космонавт")

    labels = [
        button.text
        for row in messenger.photo_edits[0].keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert labels == [texts.BUTTON_DRAW_AGAIN, texts.BUTTON_SHARE]


async def test_drawing_uses_the_quality_of_the_tariff(
    deps: Deps, session: Session, images_: FakeImages
) -> None:
    await images.draw(deps, session, "кот-космонавт")

    _, quality = images_.generated[0]
    assert quality is session.tariff.image_quality


async def test_failed_drawing_replaces_the_waiting_message(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """«Рисую…» не должно висеть вечно, если ничего не вышло."""
    images_.error = RuntimeError("провайдер лёг")

    await images.draw(deps, session, "кот-космонавт")

    assert messenger.text_edits[0].text == texts.IMAGE_ERROR
    assert messenger.text_edits[0].keyboard is not None


async def test_image_paywall_when_pictures_run_out(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    await storage.add_usage(session.user.id, session.day, images=3)

    await images.draw(deps, session, "кот")

    assert messenger.last_text.text == texts.paywall_images().text


# --- Пресеты (§2.4) ------------------------------------------------------


async def test_preset_works_in_one_step_from_photo_to_result(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§2.4: кинул фото → получил результат. Никаких уточнений."""
    await presets.apply(deps, session, PRESETS["lego"], PHOTO)

    assert messenger.texts_said() == [texts.PRESET_WORKING]
    assert len(messenger.photo_edits) == 1


async def test_preset_result_has_all_three_buttons(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await presets.apply(deps, session, PRESETS["bad_day"], PHOTO)

    labels = [
        button.text
        for row in messenger.photo_edits[0].keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert labels == [
        texts.BUTTON_DRAW_AGAIN,
        texts.BUTTON_SEND_TO_FRIEND,
        texts.BUTTON_ANOTHER_PRESET,
    ]


async def test_preset_sends_its_own_instruction_to_the_provider(
    deps: Deps, session: Session, images_: FakeImages
) -> None:
    await presets.apply(deps, session, PRESETS["lego"], PHOTO)

    instruction, _ = images_.edited[0]
    assert instruction == PRESETS["lego"].instruction


@pytest.mark.parametrize("preset_id", ["lego", "bad_day"])
async def test_both_presets_from_the_brief_work(
    deps: Deps, session: Session, messenger: FakeMessenger, preset_id: str
) -> None:
    """Критерий приёмки №6."""
    await presets.apply(deps, session, PRESETS[preset_id], PHOTO)

    assert len(messenger.photo_edits) == 1


async def test_menu_is_built_from_the_registry(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await presets.show_menu(deps, session)

    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert labels == [preset.button for preset in PRESETS.values()]


async def test_a_third_preset_needs_no_new_handler(
    deps: Deps,
    session: Session,
    messenger: FakeMessenger,
    images_: FakeImages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий приёмки A1.

    Добавляем пресет только записью в реестре — ни строчки в сценариях,
    клавиатурах или адаптерах. Если этот тест начнёт падать, значит пресеты
    перестали быть данными и превратились в код.
    """
    extra = Preset(
        id="ghost",
        button="👻 Привидение",
        invitation="Кинь фото — сделаю из тебя привидение",
        instruction="Turn the person into a friendly cartoon ghost.",
    )
    monkeypatch.setattr(registry, "PRESETS", {**PRESETS, "ghost": extra})

    await presets.show_menu(deps, session)
    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert "👻 Привидение" in labels

    actions = [
        button.action
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert parse_preset_action(actions[-1] or "") == "ghost"

    await presets.apply(deps, session, extra, PHOTO)
    assert images_.edited[-1][0] == extra.instruction


async def test_oversized_photo_is_refused_before_the_provider(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """§3.5: ограничение размера до отправки провайдеру."""
    huge = Photo(data=PNG_BYTES + b"\x00" * deps.settings.max_photo_bytes)

    await presets.apply(deps, session, PRESETS["lego"], huge)

    assert messenger.last_text.text == texts.PHOTO_TOO_BIG
    assert images_.edited == []


async def test_non_image_is_refused_before_the_provider(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """§3.5: проверка формата по сигнатуре, а не по заявленному типу."""
    fake = Photo(data="я не картинка".encode(), mime_type="image/png")

    await presets.apply(deps, session, PRESETS["lego"], fake)

    assert messenger.last_text.text == texts.PHOTO_NOT_AN_IMAGE
    assert images_.edited == []


# --- Профиль (§2.6) ------------------------------------------------------


async def test_profile_shows_real_numbers(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Критерий приёмки №8."""
    await storage.add_usage(session.user.id, session.day, messages=12, images=1)

    await profile.show(deps, session)

    assert messenger.last_text.text == (
        "Твой тариф: Бесплатный\n"
        "Сообщений сегодня: 12 из 20\n"
        "Картинок: 2\n"
        "Друзей позвал: 0"
    )


async def test_profile_counts_the_bonus_in_the_pictures_left(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Остаток — это обе корзины: подарок за друга тоже можно потратить."""
    await storage.add_bonus(session.user.id, images=5)

    await profile.show(deps, session)

    assert "Картинок: 8" in messenger.last_text.text


async def test_profile_always_offers_two_ways_out(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await profile.show(deps, session)

    assert messenger.last_text.keyboard is not None


# --- Тарифы (§2.8) -------------------------------------------------------


async def test_tariff_screen_shows_three_cards(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await tariffs.show(deps, session)

    assert len(messenger.texts) == 3
    assert "Лайт" in messenger.texts[0].text
    assert "Про" in messenger.texts[1].text
    assert "Макс" in messenger.texts[2].text


async def test_payment_stub_is_not_a_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """До фазы 8 оплаты нет, но уйти с экрана можно."""
    await tariffs.payments_not_ready(deps, session)

    assert messenger.last_text.keyboard is not None


# --- Реферальная ссылка (§2.7) -------------------------------------------


async def test_my_link_is_a_ready_message(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await referral.show_link(deps, session)

    assert messenger.last_text.text.startswith("Тут бесплатный ChatGPT")
    assert session.user.referral_code in messenger.last_text.text
