"""Тесты продуктовых сценариев.

Сценарии живут в core и ничего не знают про мессенджер: в тестах им
подсовываются фейки портов. Тот же код на фазе 7 поедет в MAX без единой
правки — это и проверяется на приёмке (критерий A4).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import pending, texts
from app.core.actions import parse_preset_action
from app.core.models import Photo, TariffId
from app.core.scenarios import (
    chat,
    images,
    presets,
    profile,
    referral,
    tariffs,
)
from app.core.scenarios.deps import Deps, Session
from app.ports.ai import ContentRefusedError
from config import presets as registry
from config.presets import PRESETS, Preset
from tests.fakes import PNG_BYTES, FakeImages, FakeLLM, FakeMessenger

PHOTO = Photo(data=PNG_BYTES)


def _paid(session: Session) -> Session:
    """Тот же человек с оплаченным и ещё не истёкшим тарифом."""
    return replace(
        session,
        user=replace(
            session.user,
            tariff=TariffId.LITE,
            tariff_expires_at=session.now + timedelta(days=30),
        ),
    )


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
    await presets.apply(deps, session, PRESETS["lego"], [PHOTO])

    assert messenger.texts_said() == [texts.PRESET_WORKING]
    assert len(messenger.photo_edits) == 1


async def test_preset_result_has_all_three_buttons(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await presets.apply(deps, session, PRESETS["bad_day"], [PHOTO])

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
    await presets.apply(deps, session, PRESETS["lego"], [PHOTO])

    instruction, _ = images_.edited[0]
    assert instruction == PRESETS["lego"].instruction


@pytest.mark.parametrize("preset_id", list(PRESETS))
async def test_every_preset_in_the_registry_works(
    deps: Deps, session: Session, messenger: FakeMessenger, preset_id: str
) -> None:
    """Критерий приёмки №6 — и он про весь реестр, а не про два первых прикола.

    Тариф платный: закрытые приколы иначе до обработки не доходят, а проверить
    надо именно её. Число снимков берётся из той же записи в реестре.
    """
    preset = PRESETS[preset_id]

    await presets.apply(deps, _paid(session), preset, [PHOTO] * preset.photos_required)

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
    # Порядок реестра сохраняется целиком, включая закрытые приколы: замок
    # ставится поверх подписи, а не вместо места в списке.
    assert labels == [
        texts.locked_button(preset.button) if preset.paid_only else preset.button
        for preset in PRESETS.values()
    ]


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
        invitations=("Кинь фото — сделаю из тебя привидение",),
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

    await presets.apply(deps, session, extra, [PHOTO])
    assert images_.edited[-1][0] == extra.instruction


async def test_a_two_photo_preset_needs_no_new_handler_either(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    images_: FakeImages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Критерий приёмки A1 для приколов, которым мало одного снимка.

    Второе приглашение, лишний шаг и порядок снимков берутся из той же
    записи в реестре. Если этот тест начнёт падать, значит «сколько фото» и
    «какими словами просить второе» снова стали кодом.
    """
    extra = Preset(
        id="twins",
        button="👯 Двое",
        invitations=("Кинь своё фото", "Теперь фото друга"),
        instruction="Put both people in one frame.",
    )
    monkeypatch.setattr(registry, "PRESETS", {**PRESETS, "twins": extra})

    await presets.pick(deps, session, extra)
    assert messenger.last_text.text == "Кинь своё фото"

    await presets.add_photo(deps, session, extra, PHOTO, "mine")
    assert messenger.last_text.text == "Теперь фото друга"
    assert images_.edited == []

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    awaited = pending.parse_await_preset(user.pending)
    assert awaited is not None

    await presets.add_photo(deps, session, extra, PHOTO, "theirs", awaited.collected)
    assert images_.edited[-1][0] == extra.instruction
    assert len(images_.edited_sources[-1]) == 2


async def test_oversized_photo_is_refused_before_the_provider(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """§3.5: ограничение размера до отправки провайдеру."""
    huge = Photo(data=PNG_BYTES + b"\x00" * deps.settings.max_photo_bytes)

    await presets.apply(deps, session, PRESETS["lego"], [huge])

    assert messenger.last_text.text == texts.PHOTO_TOO_BIG
    assert images_.edited == []


async def test_non_image_is_refused_before_the_provider(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """§3.5: проверка формата по сигнатуре, а не по заявленному типу."""
    fake = Photo(data="я не картинка".encode(), mime_type="image/png")

    await presets.apply(deps, session, PRESETS["lego"], [fake])

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


async def test_tariff_screen_is_one_message_with_three_buttons(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Тремя сообщениями тарифы не сравнить: третье вытесняет первое."""
    await tariffs.show(deps, session)

    assert len(messenger.texts) == 1
    for title in ("Лайт", "Про", "Макс"):
        assert title in messenger.last_text.text

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert [button.text for button in keyboard.rows[0]] == ["Лайт", "Про", "Макс"]


async def test_payment_stub_is_not_a_dead_end(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """До фазы 8 оплаты нет, но уйти с экрана можно."""
    await tariffs.payments_not_ready(deps, session)

    assert messenger.last_text.keyboard is not None


# --- Реферальная ссылка (§2.7) -------------------------------------------


async def test_my_link_first_explains_what_it_gives(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """§2.7: голая ссылка не объясняет, зачем её пересылать."""
    await referral.show_offer(deps, session)

    assert "+50 сообщений" in messenger.last_text.text
    assert messenger.last_text.keyboard is not None
    assert session.user.referral_code not in messenger.last_text.text


async def test_the_invitation_itself_is_ready_to_forward(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await referral.send_invitation(deps, session)

    assert messenger.last_text.text.startswith("Тут бесплатный ChatGPT")
    assert session.user.referral_code in messenger.last_text.text
    # Ничего лишнего: сообщение пересылают целиком.
    assert messenger.last_text.keyboard is None
    assert messenger.last_text.show_menu is False


# --- Отказ провайдера по содержанию --------------------------------------


async def test_a_refused_drawing_says_so_instead_of_blaming_a_failure(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """«Попробуй ещё раз» на отказе по содержанию — обещание, которое лжёт."""
    images_.error = ContentRefusedError("moderation_blocked")

    await images.draw(deps, session, "что-нибудь запрещённое")

    assert messenger.text_edits[0].text == texts.IMAGE_REFUSED
    assert messenger.text_edits[0].keyboard is None, "кнопки «Повторить» быть не должно"


async def test_a_refused_drawing_costs_nothing(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    images_: FakeImages,
) -> None:
    images_.error = ContentRefusedError("moderation_blocked")

    await images.draw(deps, session, "что-нибудь запрещённое")

    usage = await storage.get_usage(session.user.id, session.day)
    assert usage.images_used == 0


async def test_a_refused_drawing_is_not_offered_for_repeat(
    deps: Deps, session: Session, storage: InMemoryStorage, images_: FakeImages
) -> None:
    """Повтор дал бы тот же отказ — запоминать тут нечего."""
    images_.error = ContentRefusedError("moderation_blocked")

    await images.draw(deps, session, "что-нибудь запрещённое")

    stored = await storage.get_user_by_id(session.user.id)
    assert stored is not None
    assert stored.retry_context is None


async def test_a_refused_photo_keeps_a_way_out(
    deps: Deps, session: Session, messenger: FakeMessenger, images_: FakeImages
) -> None:
    """Тупика быть не должно даже на отказе: под текстом список приколов."""
    images_.error = ContentRefusedError("moderation_blocked")

    await presets.apply(deps, session, registry.PRESETS["lego"], [PHOTO], ["src"])

    assert messenger.text_edits[0].text == texts.PRESET_REFUSED
    keyboard = messenger.text_edits[0].keyboard
    assert keyboard is not None
    assert len(keyboard.rows) == len(registry.PRESETS)


# --- Номер для поддержки -------------------------------------------------


async def test_the_profile_shows_the_number_where_it_is_needed(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """В MAX username есть не у всех — без номера написавшего не опознать."""
    with_number = replace(deps, settings=replace(deps.settings, show_user_number=True))

    await profile.show(with_number, session)

    assert f"Твой номер: {session.user.support_number}" in messenger.last_text.text
    # Не внутренний идентификатор строки: по нему было бы видно, сколько
    # всего людей в сервисе.
    assert session.user.support_number != int(session.user.id)


async def test_the_profile_hides_the_number_where_it_is_not(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """В Telegram человека видно по @username, лишняя строка ни к чему."""
    await profile.show(deps, session)

    assert "Твой номер" not in messenger.last_text.text


async def test_the_profile_shows_the_tariff_that_actually_works(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """После окончания подписки профиль обязан говорить правду.

    Иначе человек видит «Твой тариф: Про», а лимиты у него бесплатные, и
    первый же вопрос в поддержку — про это расхождение.
    """
    await storage.set_tariff(
        session.user.id, TariffId.PRO, deps.now() - timedelta(seconds=1)
    )
    expired = await storage.get_user_by_id(session.user.id)
    assert expired is not None

    await profile.show(deps, replace(session, user=expired))

    assert "Твой тариф: Бесплатный" in messenger.last_text.text


# --- Замок на платных приколах -------------------------------------------


@pytest.fixture
def locked() -> Preset:
    """Прикол, закрытый до покупки подписки."""
    return PRESETS["figurine"]


async def test_a_locked_preset_stays_in_the_menu(
    deps: Deps, session: Session, messenger: FakeMessenger, locked: Preset
) -> None:
    """Замок — витрина, а не забор: прятать причину купить подписку незачем."""
    await presets.show_menu(deps, session)

    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert texts.locked_button(locked.button) in labels
    assert locked.button not in labels


async def test_a_paid_tariff_takes_the_lock_off(
    deps: Deps, session: Session, messenger: FakeMessenger, locked: Preset
) -> None:
    paid = _paid(session)

    await presets.show_menu(deps, paid)

    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert locked.button in labels
    assert texts.locked_button(locked.button) not in labels


async def test_an_expired_tariff_puts_the_lock_back(
    deps: Deps, session: Session, messenger: FakeMessenger, locked: Preset
) -> None:
    """Замок снимает действующий тариф, а не записанный когда-то."""
    expired = replace(
        session,
        user=replace(
            session.user,
            tariff=TariffId.PRO,
            tariff_expires_at=session.now - timedelta(days=1),
        ),
    )

    await presets.show_menu(deps, expired)

    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert texts.locked_button(locked.button) in labels


async def test_tapping_a_locked_preset_offers_the_tariffs(
    deps: Deps, session: Session, messenger: FakeMessenger, locked: Preset
) -> None:
    """Человек сам показал, за что готов заплатить, — лучший момент продать."""
    await presets.pick(deps, session, locked)

    assert messenger.last_text.text == texts.PRESET_LOCKED
    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert labels == [texts.BUTTON_OPEN_TARIFFS, texts.BUTTON_ANOTHER_PRESET]


async def test_a_locked_preset_does_not_start_waiting_for_a_photo(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    locked: Preset,
) -> None:
    """Иначе следующее фото ушло бы в обработку в обход замка."""
    await presets.pick(deps, session, locked)

    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert user.pending is None


async def test_a_paid_user_gets_asked_for_the_photo(
    deps: Deps,
    session: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    locked: Preset,
) -> None:
    paid = _paid(session)

    await presets.pick(deps, paid, locked)

    assert messenger.last_text.text == locked.invitations[0]
    user = await storage.get_user_by_id(session.user.id)
    assert user is not None
    assert pending.parse_await_preset(user.pending) == pending.AwaitedPreset("figurine")


# --- Прикол из двух фото -------------------------------------------------


@pytest.fixture
def two_photos() -> Preset:
    return PRESETS["polaroid_child"]


@pytest.fixture
def paid(session: Session) -> Session:
    """Тариф, на котором открыты все приколы."""
    return _paid(session)


async def test_the_first_photo_only_asks_for_the_second(
    deps: Deps,
    paid: Session,
    messenger: FakeMessenger,
    images_: FakeImages,
    two_photos: Preset,
) -> None:
    """Работать ещё нечем: одного снимка для этого прикола мало."""
    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult-ref")

    assert messenger.last_text.text == two_photos.invitations[1]
    assert images_.edited == []


async def test_the_second_photo_step_can_be_cancelled(
    deps: Deps, paid: Session, messenger: FakeMessenger, two_photos: Preset
) -> None:
    """Человек уже что-то отдал боту — уйти молча он не должен быть обязан."""
    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult-ref")

    labels = [
        button.text
        for row in messenger.last_text.keyboard.rows  # type: ignore[union-attr]
        for button in row
    ]
    assert labels == [texts.BUTTON_CANCEL]


async def test_the_first_photo_is_remembered_until_the_second_arrives(
    deps: Deps, paid: Session, storage: InMemoryStorage, two_photos: Preset
) -> None:
    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult-ref")

    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert pending.parse_await_preset(user.pending) == pending.AwaitedPreset(
        "polaroid_child", ("adult-ref",)
    )


async def test_both_photos_go_to_the_provider_in_one_request(
    deps: Deps,
    paid: Session,
    images_: FakeImages,
    messenger: FakeMessenger,
    two_photos: Preset,
) -> None:
    """Порядок существенный: инструкция ссылается на снимки по номерам."""
    child = Photo(data=PNG_BYTES, filename="child.png")

    await presets.add_photo(deps, paid, two_photos, child, "child-ref", ("adult-ref",))

    assert len(images_.edited) == 1
    assert len(images_.edited_sources[0]) == 2
    # Взрослый снимок первым — провайдер вытягивает детали первого сильнее.
    assert messenger.downloaded == ["adult-ref"]
    assert images_.edited_sources[0][1] is child


async def test_a_lost_first_photo_starts_the_collection_over(
    deps: Deps,
    paid: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    images_: FakeImages,
    two_photos: Preset,
) -> None:
    """В MAX ссылка на снимок живёт не вечно, а человек может уйти надолго."""
    messenger.fail_download = RuntimeError("ссылка протухла")

    await presets.add_photo(deps, paid, two_photos, PHOTO, "child-ref", ("adult-ref",))

    assert messenger.last_text.text == texts.PRESET_PHOTO_LOST
    assert images_.edited == []
    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert user.pending is None


async def test_a_lost_first_photo_is_not_reported_as_a_breakdown(
    deps: Deps, paid: Session, messenger: FakeMessenger, two_photos: Preset
) -> None:
    """Обрабатывать было нечего — говорить об ошибке обработки было бы неправдой."""
    messenger.fail_download = RuntimeError("ссылка протухла")

    await presets.add_photo(deps, paid, two_photos, PHOTO, "child-ref", ("adult-ref",))

    assert texts.PRESET_ERROR not in messenger.texts_said()


async def test_the_second_photo_is_not_asked_for_without_images_left(
    deps: Deps,
    paid: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    two_photos: Preset,
) -> None:
    """Иначе человек прислал бы второй снимок впустую."""
    await storage.add_usage(paid.user.id, paid.day, messages=0, images=40)

    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult-ref")

    assert messenger.last_text.text != two_photos.invitations[1]
    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert user.pending is None


async def test_an_unsuitable_first_photo_does_not_move_the_step_on(
    deps: Deps,
    paid: Session,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    two_photos: Preset,
) -> None:
    """§3.5: проверка формата до всего остального, и на каждом снимке."""
    fake = Photo(data="я не картинка".encode(), mime_type="image/png")

    await presets.add_photo(deps, paid, two_photos, fake, "adult-ref")

    assert messenger.last_text.text == texts.PHOTO_NOT_AN_IMAGE
    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert user.pending is None


async def test_a_finished_pair_is_forgotten_before_the_next_one(
    deps: Deps,
    paid: Session,
    storage: InMemoryStorage,
    images_: FakeImages,
    two_photos: Preset,
) -> None:
    """Иначе следующий снимок приклеился бы к уже отработанному первому.

    Прикол остаётся выбранным — человек может прислать подряд ещё одну пару, —
    но собранное обнуляется: два снимка отработаны, третий начинает новую пару.
    """
    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult", ("first",))

    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert pending.parse_await_preset(user.pending) == pending.AwaitedPreset(
        "polaroid_child"
    )


async def test_a_failed_pair_is_forgotten_too(
    deps: Deps,
    paid: Session,
    storage: InMemoryStorage,
    images_: FakeImages,
    two_photos: Preset,
) -> None:
    """Провайдер упал — отработанный снимок всё равно не должен ждать напарника."""
    images_.error = RuntimeError("провайдер лёг")

    await presets.add_photo(deps, paid, two_photos, PHOTO, "adult", ("first",))

    user = await storage.get_user_by_id(paid.user.id)
    assert user is not None
    assert pending.parse_await_preset(user.pending) == pending.AwaitedPreset(
        "polaroid_child"
    )
