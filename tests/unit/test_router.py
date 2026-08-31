"""Маршрутизация: что происходит с обращением до того, как начнётся сценарий.

Тут проверяются решения, общие для обоих мессенджеров, — те самые, из-за
которых на фазе 7 MAX-адаптеру не понадобится ни строчки в core/.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import pending, texts
from app.core.actions import Action, buy_action, preset_action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from tests.fakes import FakeGuard, FakeImages, FakeLLM, FakeMessenger

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")


def incoming(**fields: object) -> IncomingMessage:
    """Обращение от пользователя «1» — того же, что заводит фикстура user."""
    return IncomingMessage(
        chat=CHAT,
        external_user_id="1",
        **fields,  # type: ignore[arg-type]
    )


# --- Знакомство ----------------------------------------------------------


async def test_start_greets(deps: Deps, messenger: FakeMessenger) -> None:
    await handle(deps, incoming(start_payload=""))

    assert messenger.texts_said()[0].startswith("Привет! Я отвечу на любой вопрос")


async def test_unknown_user_is_greeted_instead_of_ignored(
    deps: Deps, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """Человек пишет, не нажав /start: молчать нельзя, падать — тем более."""
    await handle(deps, incoming(text="привет"))

    assert messenger.texts_said()
    assert await storage.get_user(MessengerKind.TELEGRAM, "1") is not None


async def test_start_payload_still_works_for_a_known_user(
    deps: Deps, user: User, messenger: FakeMessenger, storage: InMemoryStorage
) -> None:
    """Повторный /start здоровается заново, но подарков не раздаёт."""
    await handle(deps, incoming(start_payload="ref_whatever"))

    assert messenger.texts_said()
    refreshed = await storage.get_user_by_id(user.id)
    assert refreshed is not None
    assert refreshed.bonus_images == 0


# --- Кнопки --------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (Action.MENU_IMAGES, texts.IMAGE_ASK),
        (Action.MENU_PRESETS, texts.PRESETS_ASK),
        (Action.PRESET_ANOTHER, texts.PRESETS_ASK),
    ],
)
async def test_menu_actions_open_their_screens(
    deps: Deps, user: User, messenger: FakeMessenger, action: str, expected: str
) -> None:
    await handle(deps, incoming(action=action))

    assert messenger.texts_said() == [expected]


async def test_menu_label_arriving_as_text_is_recognised(
    deps: Deps, user: User, messenger: FakeMessenger, llm: FakeLLM
) -> None:
    """Постоянная клавиатура Telegram присылает подпись, а не действие."""
    await handle(deps, incoming(text=texts.MENU_IMAGES))

    assert messenger.texts_said() == [texts.IMAGE_ASK]
    assert llm.calls == []


async def test_buy_action_asks_how_to_pay(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Способов два — значит сначала спрашиваем, чем платить.

    Условия здесь ещё не показываются: они на экране заказа, вместе с
    кнопкой оплаты, которой человек и даёт согласие.
    """
    await handle(deps, incoming(action=buy_action("pro")))

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    assert [button.text for button in keyboard.rows[0]] == [
        texts.BUTTON_PAY_CARD,
        texts.BUTTON_PAY_STARS,
    ]


async def test_an_unconfigured_payment_is_an_honest_stub(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Без ключей провайдера и без звёзд тупика всё равно быть не должно."""
    await handle(
        replace(deps, cards=None, stars=None), incoming(action=buy_action("pro"))
    )

    assert messenger.texts_said() == [texts.PAYMENTS_SOON]


async def test_a_tariff_that_no_longer_exists_does_not_dead_end(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Кнопка из версии, где тариф назывался иначе."""
    await handle(deps, incoming(action=buy_action("platinum")))

    assert messenger.texts_said()


async def test_unknown_action_does_not_leave_the_user_in_silence(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Кнопка из версии, которой больше нет."""
    await handle(deps, incoming(action="x:gone"))

    assert messenger.texts_said() == [texts.UNSUPPORTED_INPUT]


async def test_callback_is_acknowledged_before_the_work(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Иначе «часики» на кнопке крутятся все пятнадцать секунд отрисовки."""
    await handle(deps, incoming(action=Action.MENU_PROFILE, callback_id="cb1"))

    assert messenger.answered_callbacks == ["cb1"]


async def test_failed_acknowledgement_does_not_cancel_the_work(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Потерять ответ из-за неподтверждённой кнопки было бы обидно."""

    async def explode(callback_id: str, *, notification: str | None = None) -> None:
        raise RuntimeError("подтверждение не прошло")

    messenger.answer_callback = explode  # type: ignore[method-assign]

    await handle(deps, incoming(action=Action.MENU_PROFILE, callback_id="cb1"))

    assert messenger.texts_said()


# --- Режимы ожидания -----------------------------------------------------


async def test_images_button_switches_the_next_message_to_a_description(
    deps: Deps, user: User, storage: InMemoryStorage, images_: FakeImages
) -> None:
    await handle(deps, incoming(action=Action.MENU_IMAGES))

    stored = await storage.get_user_by_id(user.id)
    assert stored is not None
    assert pending.is_awaiting_image_prompt(stored.pending)

    await handle(deps, incoming(text="кот-космонавт"))
    assert images_.generated[0][0] == "кот-космонавт"


async def test_the_description_mode_lasts_exactly_one_message(
    deps: Deps, user: User, llm: FakeLLM, images_: FakeImages
) -> None:
    """Иначе из режима картинок нечем выйти: кнопки «в чат» в меню нет."""
    await handle(deps, incoming(action=Action.MENU_IMAGES))
    await handle(deps, incoming(text="кот-космонавт"))
    await handle(deps, incoming(text="а сколько будет два плюс два?"))

    assert len(images_.generated) == 1
    assert len(llm.calls) == 1


async def test_text_instead_of_a_photo_goes_to_chat(
    deps: Deps, user: User, llm: FakeLLM
) -> None:
    """Ждали фото, а человек передумал и написал. Запирать его нельзя."""
    await handle(deps, incoming(action=preset_action("lego")))
    await handle(deps, incoming(text="ой, не хочу"))

    assert len(llm.calls) == 1


async def test_a_photo_keeps_the_chosen_preset(
    deps: Deps, user: User, images_: FakeImages
) -> None:
    """Несколько фото подряд под тем же приколом — без переспрашивания."""
    await handle(deps, incoming(action=preset_action("lego")))
    await handle(deps, incoming(photo_ref="p1"))
    await handle(deps, incoming(photo_ref="p2"))

    assert len(images_.edited) == 2


async def test_a_photo_without_a_preset_offers_the_menu(
    deps: Deps, user: User, messenger: FakeMessenger, images_: FakeImages
) -> None:
    await handle(deps, incoming(photo_ref="p1"))

    assert messenger.texts_said() == [texts.PRESETS_ASK]
    assert images_.edited == []


async def test_a_removed_preset_does_not_dead_end(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Кнопка пресета, которого больше нет в реестре."""
    await handle(deps, incoming(action=preset_action("no_such_preset")))

    assert messenger.texts_said() == [texts.PRESETS_ASK]


# --- Непонятое -----------------------------------------------------------


async def test_a_sticker_gets_an_answer(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    await handle(deps, incoming())

    assert messenger.texts_said() == [texts.UNSUPPORTED_INPUT]


# --- Ограничитель одновременных задач ------------------------------------


async def test_a_second_simultaneous_request_is_refused(
    deps: Deps, user: User, messenger: FakeMessenger, guard: FakeGuard, llm: FakeLLM
) -> None:
    """Занятый слот — не ошибка, а честное «дождись предыдущего»."""
    assert guard.try_acquire(f"text:{int(user.id)}")

    await handle(deps, incoming(text="привет"))

    assert messenger.texts_said() == [texts.STILL_WORKING]
    assert llm.calls == []


async def test_the_slot_is_released_even_when_the_scenario_fails(
    deps: Deps, user: User, guard: FakeGuard, llm: FakeLLM
) -> None:
    """Иначе один сбой запирает пользователя навсегда."""
    llm.error = RuntimeError("провайдер лёг")

    await handle(deps, incoming(text="привет"))

    assert guard.active(f"text:{int(user.id)}") == 0


async def test_repeat_shares_the_slot_with_drawing(
    deps: Deps, user: User, messenger: FakeMessenger, guard: FakeGuard
) -> None:
    """Иначе зажатая кнопка «Ещё раз» обходила бы ограничитель другим ключом."""
    assert guard.try_acquire(f"image:{int(user.id)}")

    await handle(deps, incoming(action=Action.IMAGE_AGAIN))

    assert messenger.texts_said() == [texts.STILL_WORKING]


async def test_a_refused_slot_does_not_eat_the_description_mode(
    deps: Deps, user: User, guard: FakeGuard, storage: InMemoryStorage
) -> None:
    """Отказ «дождись предыдущего» не должен стоить человеку режима.

    Иначе он остаётся и без картинки, и без режима: следующее сообщение
    уедет в чат, хотя он просил нарисовать.
    """
    await handle(deps, incoming(action=Action.MENU_IMAGES))
    assert guard.try_acquire(f"image:{int(user.id)}")

    await handle(deps, incoming(text="кот-космонавт"))

    stored = await storage.get_user_by_id(user.id)
    assert stored is not None
    assert pending.is_awaiting_image_prompt(stored.pending)


async def test_the_referral_button_leads_to_the_offer_then_the_invitation(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Обе кнопки — из профиля и с пейволла — ведут в одно и то же место."""
    await handle(deps, incoming(action=Action.MY_LINK))
    await handle(deps, incoming(action=Action.INVITE_FRIEND))

    from_profile, from_paywall = messenger.texts_said()
    assert from_profile == from_paywall
    assert "+50 сообщений" in from_profile

    await handle(deps, incoming(action=Action.REFERRAL_SEND))

    assert messenger.texts_said()[-1].startswith("Тут бесплатный ChatGPT")
