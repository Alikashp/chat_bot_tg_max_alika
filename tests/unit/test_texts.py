"""Тесты текстов и линтера — критерий приёмки №11.

Половина этих тестов проверяет не тексты, а сам линтер: проверка, которая
всегда проходит, ничего не стоит. Поэтому по каждому правилу §2.9 есть тест,
что нарушение действительно ловится.
"""

from __future__ import annotations

import pytest

from app.core import texts
from app.core.models import TariffId
from app.core.scenarios import keyboards as scenario_keyboards
from app.core.texts import Screen
from scripts.check_texts import check_presets, check_screen, collect

#: Сколько символов помещается в ряд кнопок на телефоне.
#: Число подобрано по живому прогону: ряд из трёх кнопок общей длиной 43
#: обрезался до «Отп…другу», ряд из двух длиной 28 читается целиком.
MAX_ROW_CHARS = 30

# --- Тексты соответствуют заданию дословно -------------------------------


def test_onboarding_matches_the_brief() -> None:
    """§2.1: ровно три строки, дословно."""
    screen = texts.onboarding(daily_messages=20, daily_images=3)

    assert screen.lines == [
        "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку.",
        "Просто напиши мне что-нибудь 👇",
        "У тебя 20 сообщений в день и 3 картинки бесплатно.",
    ]


def test_onboarding_from_presentations_replaces_the_first_line() -> None:
    """§2.1: ветка deeplink pres_* — другая первая строка и квота 5."""
    screen = texts.onboarding(
        daily_messages=20, daily_images=5, from_presentations=True
    )

    assert screen.lines[0] == (
        "Привет! Ты из бота презентаций — здесь ещё чат и картинки. "
        "Держи 5 картинок вместо 3 за переход."
    )
    assert screen.lines[2] == "У тебя 20 сообщений в день и 5 картинок бесплатно."


def test_onboarding_mentions_the_gift_from_a_friend() -> None:
    """§2.7: приглашённый должен сразу понять, откуда у него больше лимитов."""
    screen = texts.onboarding(daily_messages=20, daily_images=3, referral_gift=True)

    assert screen.lines[3] == "Тебе подарок от друга: +50 сообщений и +5 картинок."


def test_chat_error_promises_the_message_was_not_spent() -> None:
    """§2.2: обещание в тексте обязано быть правдой — это проверяет сценарий."""
    screen = texts.chat_error()

    assert screen.text == (
        "Что-то пошло не так, попробуй ещё раз 🤷 Сообщение не потратилось."
    )
    assert screen.buttons == ("Повторить",)


def test_paywall_always_offers_two_ways_out() -> None:
    """§2.5: тупика быть не должно никогда."""
    for screen in (texts.paywall_images(), texts.paywall_messages()):
        assert len(screen.buttons) == 2


def test_paywall_images_matches_the_brief() -> None:
    screen = texts.paywall_images()

    assert screen.lines == [
        "Картинки на сегодня закончились 😔",
        "Завтра будет ещё одна, а можно не ждать:",
    ]
    assert screen.buttons == (
        "⭐ Открыть тарифы",
        "🎁 Позвать друга → +5 картинок сразу",
    )


def test_profile_shows_four_numbers() -> None:
    """§2.6: все четыре числа настоящие."""
    screen = texts.profile(
        tariff_id=TariffId.FREE,
        messages_used=12,
        messages_limit=20,
        images_left=2,
        friends=3,
    )

    assert screen.lines == [
        "Твой тариф: Бесплатный",
        "Сообщений сегодня: 12 из 20",
        "Картинок: 2",
        "Друзей позвал: 3",
    ]


def test_referral_invite_is_a_ready_message_not_a_bare_link() -> None:
    """§2.7: пользователю остаётся одно действие — переслать."""
    screen = texts.referral_invite("https://t.me/bot?start=ref_x")

    assert screen.text == (
        "Тут бесплатный ChatGPT и картинки, "
        "без VPN и регистрации 👉 https://t.me/bot?start=ref_x"
    )


def test_share_caption_carries_the_personal_link() -> None:
    """§2.3: это виральный канал, а не опция."""
    screen = texts.share_caption("mybot", "https://t.me/mybot?start=ref_abc")

    assert "@mybot" in screen.text
    assert "ref_abc" in screen.text


def test_pro_card_is_marked_as_popular() -> None:
    assert "⭐ популярный" in texts.tariff_card(TariffId.PRO).text


def test_max_price_is_formatted_with_a_space() -> None:
    assert "1 490 ₽/мес" in texts.tariff_card(TariffId.MAX).text


# --- Согласование числительных -------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 картинка"),
        (2, "2 картинки"),
        (3, "3 картинки"),
        (5, "5 картинок"),
        (11, "11 картинок"),
        (21, "21 картинка"),
        (40, "40 картинок"),
        (102, "102 картинки"),
        (150, "150 картинок"),
    ],
)
def test_images_are_pluralised_correctly(count: int, expected: str) -> None:
    """«5 картинки» в интерфейсе выглядит как недоделка."""
    screen = texts.onboarding(daily_messages=20, daily_images=count)

    assert expected in screen.text


# --- Линтер ловит нарушения ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Осталось 5 кредитов",
        "Потрачено токенов: 10",
        "Наша нейросеть подумает",
        "Напиши промпт",
        "Генерация займёт 15 секунд",
        "Генерируем картинку",
    ],
)
def test_linter_catches_forbidden_words(text: str) -> None:
    violations = check_screen(Screen(text=text, buttons=("Дальше",)))

    assert any(v.rule == "запрещённое слово" for v in violations)


@pytest.mark.parametrize(
    "text",
    ["Вы уверены?", "Ваш тариф закончился", "Пришлите нам ваше фото"],
)
def test_linter_catches_formal_address(text: str) -> None:
    violations = check_screen(Screen(text=text, buttons=("Дальше",)))

    assert any(v.rule == "обращение" for v in violations)


@pytest.mark.parametrize("text", ["Выбери тариф", "Выход есть всегда", "Выключи"])
def test_linter_does_not_trip_on_words_starting_with_vy(text: str) -> None:
    """«Выбери» — обращение на «ты», а не на «вы»."""
    violations = check_screen(Screen(text=text, buttons=("Дальше",)))

    assert not any(v.rule == "обращение" for v in violations)


def test_linter_catches_long_messages() -> None:
    violations = check_screen(Screen(text="\n".join("строка" for _ in range(6))))

    assert any(v.rule == "длина" for v in violations)


def test_linter_catches_a_dead_end() -> None:
    """Экран без кнопок и без следующего шага — тупик."""
    violations = check_screen(Screen(text="Просто текст"))

    assert any(v.rule == "тупик" for v in violations)


def test_linter_accepts_a_screen_with_an_explicit_next_step() -> None:
    screen = Screen(text="Кинь фото", next_step="ждём фото")

    assert check_screen(screen) == []


def test_linter_checks_button_labels_too() -> None:
    violations = check_screen(Screen(text="Всё хорошо", buttons=("Купить кредиты",)))

    assert any(v.rule == "запрещённое слово" for v in violations)


# --- Все настоящие тексты проходят проверку ------------------------------


def test_every_screen_passes_the_linter() -> None:
    assert collect() == []


def test_every_preset_passes_the_linter() -> None:
    assert check_presets() == []


def test_no_screen_is_longer_than_five_lines() -> None:
    too_long = [screen for screen in texts.SCREENS if len(screen.lines) > 5]

    assert too_long == []


def test_every_screen_has_a_way_out() -> None:
    """§2.9: ни одного экрана, с которого нельзя уйти."""
    stuck = [
        screen
        for screen in texts.SCREENS
        if not screen.buttons and not screen.next_step
    ]

    assert stuck == []


def test_no_row_of_buttons_is_too_wide_for_a_phone() -> None:
    """Три подписи в строку на телефоне обрезаются до нечитаемого огрызка.

    Проверка появилась после живого прогона: под обработанным фото выходили
    «Отп…другу» и «Др…рикол» — по таким подписям не понять, что делает кнопка.
    """
    keyboards = {
        "меню": scenario_keyboards.main_menu(),
        "картинка": scenario_keyboards.image_result(),
        "прикол": scenario_keyboards.preset_result(),
        "пейволл": scenario_keyboards.paywall(texts.BUTTON_INVITE_FOR_IMAGES),
        "профиль": scenario_keyboards.profile(),
        "оплата": scenario_keyboards.payments_soon(),
    }

    for name, keyboard in keyboards.items():
        for row in keyboard.rows:
            assert len(row) <= 2, f"{name}: три кнопки в ряду не поместятся"
            if len(row) < 2:
                # Одинокая кнопка занимает всю ширину, и её подпись
                # переносится, а не обрезается.
                continue
            width = sum(len(button.text) for button in row)
            assert width <= MAX_ROW_CHARS, f"{name}: ряд длиной {width} не поместится"
