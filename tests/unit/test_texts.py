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
from app.core.tariffs import RUB
from app.core.texts import Screen
from scripts.check_texts import check_presets, check_screen, collect

#: Сколько символов помещается в ряд кнопок на телефоне.
#: Число подобрано по живому прогону: ряд из трёх кнопок общей длиной 43
#: обрезался до «Отп…другу», ряд из двух длиной 28 читается целиком.
MAX_ROW_CHARS = 30

# --- Тексты соответствуют заданию дословно -------------------------------


def test_onboarding_matches_the_brief() -> None:
    """§2.1: две строки — поздороваться и позвать написать."""
    screen = texts.onboarding()

    assert screen.lines == [
        "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку.",
        "Просто напиши мне что-нибудь 👇",
    ]


def test_onboarding_does_not_report_limits() -> None:
    """Решение заказчика: первый экран зовёт попробовать, а не отчитывается.

    Человек ещё ничего не сделал, а ему уже называют, сколько ему можно.
    Свои остатки он видит в профиле, и там они всегда свежие.
    """
    plain = texts.onboarding().text

    assert not any(char.isdigit() for char in plain)
    assert "в день" not in plain


def test_onboarding_from_presentations_replaces_the_first_line() -> None:
    """§2.1: ветка deeplink pres_* — другая первая строка."""
    screen = texts.onboarding(from_presentations=True)

    assert screen.lines[0] == (
        "Привет! Ты из бота презентаций — здесь ещё чат и картинки. "
        "Держи бонусные картинки за переход."
    )


def test_the_referral_offer_promises_the_friend_nothing() -> None:
    """Фаза 10, К7: другу ничего не начисляется — значит, и не обещается."""
    text = texts.referral_offer(bonus_messages=20, bonus_images=2).text

    assert "+20 сообщений" in text
    assert "+2 картинки" in text
    assert "Другу" not in text
    assert "подар" not in text


def test_chat_error_promises_the_message_was_not_spent() -> None:
    """§2.2: обещание в тексте обязано быть правдой — это проверяет сценарий."""
    screen = texts.chat_error()

    assert screen.text == (
        "Что-то пошло не так, попробуй ещё раз 🤷 Сообщение не потратилось."
    )
    assert screen.buttons == ("Повторить",)


def test_paywall_always_offers_two_ways_out() -> None:
    """§2.5: тупика быть не должно никогда."""
    screens = (
        texts.paywall_images(renews_on="27 сентября", invite_images=2),
        texts.paywall_images(renews_on=None, invite_images=2),
        texts.paywall_presentations(1, renews_on="27 сентября"),
        texts.paywall_presentations(1, renews_on=None),
        texts.paywall_messages(invite_messages=50),
    )

    for screen in screens:
        assert len(screen.buttons) >= 2


def test_the_image_paywall_names_the_day_new_ones_come() -> None:
    """Месячная норма: «завтра будут ещё» было бы обманом на любом тарифе."""
    screen = texts.paywall_images(renews_on="27 сентября", invite_images=2)

    assert screen.lines == [
        "Картинки на этот месяц закончились 😔",
        "Новые будут 27 сентября, а можно не ждать:",
    ]


def test_a_norm_that_needs_the_renewal_says_so() -> None:
    """Новая норма зависит от списания — экран не выдаёт её за обещанную."""
    screen = texts.paywall_documents(renews_on="27 сентября", by_charge=True)

    assert screen.lines[1] == "Новые придут с продлением 27 сентября, а можно не ждать:"


def test_paywall_names_the_reward_it_actually_gives() -> None:
    """Число в подписи приходит из настроек: обещать надо то, что начислим."""
    screen = texts.paywall_images(renews_on="27 сентября", invite_images=2)

    assert screen.buttons == (
        "⭐ Открыть тарифы",
        "🎁 Позвать друга → +2 картинки сразу",
    )


def test_the_paywall_promises_nothing_for_the_channel() -> None:
    """Бонус за канал убран (сессия 8, Г6) — и кнопки за него нет."""
    screen = texts.paywall_images(renews_on="27 сентября", invite_images=2)

    assert screen.buttons == (
        "⭐ Открыть тарифы",
        "🎁 Позвать друга → +2 картинки сразу",
    )


def test_profile_shows_every_number() -> None:
    """§4.7: остатки по тому, что есть в меню, одной строкой: экран
    ограничен пятью, а номер для поддержки в MAX берёт пятую."""
    screen = texts.profile(
        tariff_id=TariffId.FREE,
        messages_used=12,
        messages_limit=20,
        images_left=6,
        documents_left=2,
        presentations_left=1,
        friends=3,
        user_number=123456,
        period_ends="27 сентября",
    )

    assert screen.lines == [
        "Твой тариф: Бесплатный · новые картинки 27 сентября",
        "Сообщений сегодня: 12 из 20",
        "Картинки: 6 · Доклад / Реферат: 2 · Презентации: 1",
        "Друзей позвал: 3",
        "Твой номер: 123456",
    ]


def test_profile_labels_are_the_menu_buttons() -> None:
    """Д5: подписи остатков — названия кнопок меню, а не «разборы»."""
    line = texts.profile(
        tariff_id=TariffId.FREE,
        messages_used=0,
        messages_limit=20,
        images_left=0,
        documents_left=0,
        presentations_left=0,
        friends=0,
    ).lines[2]

    for button in (texts.MENU_IMAGES, texts.MENU_DOCUMENTS, texts.MENU_PRESENTATIONS):
        assert button.split(" ", 1)[1] in line
    assert "разбор" not in line.lower()


def test_profile_without_presentations_does_not_mention_them() -> None:
    """Без ключа API презентаций нет и в профиле (§4.10)."""
    line = texts.profile(
        tariff_id=TariffId.FREE,
        messages_used=0,
        messages_limit=20,
        images_left=3,
        documents_left=3,
        friends=0,
    ).lines[2]

    assert line == "Картинки: 3 · Доклад / Реферат: 3"


def test_referral_invite_is_a_ready_message_not_a_bare_link() -> None:
    """§2.7: пользователю остаётся одно действие — переслать."""
    screen = texts.referral_invite("https://t.me/bot?start=ref_x")

    assert screen.text == (
        "Тут бесплатный ChatGPT и картинки, "
        "без регистрации 👉 https://t.me/bot?start=ref_x"
    )


def test_the_invitation_does_not_promise_anything_about_vpn() -> None:
    """Телеграм в России и так открывают через VPN — обещание пустое."""
    screen = texts.referral_invite("https://t.me/bot?start=ref_x")

    assert "VPN" not in screen.text


def test_the_referral_offer_names_the_reward() -> None:
    """§2.7: сначала выгода, потом ссылка. Иначе непонятно, зачем пересылать."""
    screen = texts.referral_offer(bonus_messages=50, bonus_images=2)

    assert "+50 сообщений" in screen.text
    assert "+2 картинки" in screen.text
    assert screen.buttons == (texts.BUTTON_SEND_TO_FRIEND,)


def test_share_caption_carries_the_personal_link() -> None:
    """§2.3: это виральный канал, а не опция."""
    screen = texts.share_caption("mybot", "https://t.me/mybot?start=ref_abc")

    assert "@mybot" in screen.text
    assert "ref_abc" in screen.text


def test_pro_is_marked_as_the_one_people_take() -> None:
    assert texts.POPULAR_MARK in texts.tariffs_screen(with_presentations=True).text


def test_the_popular_mark_is_not_a_star() -> None:
    """Звезда в Telegram — валюта, и «⭐ популярный» читается как цена."""
    assert "⭐" not in texts.POPULAR_MARK


def test_max_price_is_formatted_with_a_space() -> None:
    assert "1 490 ₽/мес" in texts.tariffs_screen(with_presentations=True).text


def test_all_three_tariffs_fit_one_screen() -> None:
    """Сравнивают глазами: три отдельных сообщения сравнить нельзя."""
    screen = texts.tariffs_screen(with_presentations=True)

    for title in ("Лайт", "Про", "Макс"):
        assert title in screen.text
    assert screen.buttons == ("Лайт", "Про", "Макс")


#: Карточки тарифов из поручения фазы 11, часть 2 — дословно (Т6).
TARIFF_CARDS = (
    "⚡️ Лайт — 299 ₽/мес\n"
    "· 100 сообщений в день\n"
    "· 20 картинок в месяц\n"
    "· 10 презентаций в месяц\n"
    "· 15 докладов в месяц\n"
    "\n"
    "🚀 Про — 599 ₽/мес · берут чаще всего\n"
    "· 100 сообщений в день\n"
    "· 40 картинок в месяц\n"
    "· 25 презентаций в месяц\n"
    "· 40 докладов в месяц\n"
    "\n"
    "💥 Макс — 1 490 ₽/мес\n"
    "· 200 сообщений в день\n"
    "· 150 картинок в месяц\n"
    "· 60 презентаций в месяц\n"
    "· 100 докладов в месяц"
)


def test_the_tariff_cards_are_word_for_word() -> None:
    """Т6: карточки — ровно те, что дал заказчик."""
    assert texts.tariffs_screen(with_presentations=True).text == TARIFF_CARDS


def test_without_presentations_the_cards_do_not_promise_them() -> None:
    """Т6: без ключа API презентаций раздела нет — и в карточках о нём ни слова."""
    screen = texts.tariffs_screen(with_presentations=False)

    assert "презентац" not in screen.text
    expected = "\n".join(
        line for line in TARIFF_CARDS.split("\n") if "презентац" not in line
    )
    assert screen.text == expected


def test_nothing_that_does_not_exist_is_sold() -> None:
    """Голосового ввода и видео в боте нет — и на витрине их нет."""
    for with_presentations in (True, False):
        text = texts.tariffs_screen(with_presentations=with_presentations).text
        assert "голосов" not in text
        assert "видео" not in text


def test_the_cards_follow_the_registry() -> None:
    """Числа на карточках берутся из реестра тарифов, а не пишутся второй раз."""
    from app.core.tariffs import tariff_of

    lines = texts.tariffs_screen(with_presentations=True).lines
    for tariff_id in (TariffId.LITE, TariffId.PRO, TariffId.MAX):
        tariff = tariff_of(tariff_id)
        assert f"· {tariff.monthly_images} картинок в месяц" in lines


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
    assert expected in texts.button_invite_for_images(count)


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
    too_long = [
        screen for screen in texts.SCREENS if len(screen.lines) > screen.max_lines
    ]

    assert too_long == []


def test_only_the_tariff_and_deck_screens_raise_the_line_limit() -> None:
    """Исключение из правила §2.9 — ровно два экрана, и оба названы здесь.

    Потолок в пять строк легко обойти, подняв max_lines «на этот раз».
    Тест делает такое обход видимым: список исключений один и лежит здесь.
    Экран тарифов — сравнение трёх карточек; экран параметров презентации —
    всё, что соберётся, одним взглядом (разрешено заказчиком, сессия 7).
    """
    exceptions = [screen for screen in texts.SCREENS if screen.max_lines != 5]

    tariffs = [s for s in exceptions if s.buttons == ("Лайт", "Про", "Макс")]
    deck = [s for s in exceptions if s.buttons[:1] == (texts.BUTTON_DECK_BUILD,)]
    assert len(tariffs) + len(deck) == len(exceptions)
    assert tariffs and deck
    assert all(s.max_lines == 8 for s in deck)


def test_only_the_consent_screen_may_say_you() -> None:
    """Исключение из правила §2.9 должно оставаться ровно одним.

    Обращение на «вы» снято там, где человек становится стороной договора:
    «нажимая кнопку, ты соглашаешься с офертой» звучало бы как приятельская
    просьба, а не как согласие с условиями. Послабление легко расползётся по
    другим экранам, поэтому список исключений один и лежит здесь.
    """
    exceptions = [screen for screen in texts.SCREENS if screen.formal_address]

    assert {screen.text for screen in exceptions} != set()
    assert all(texts.CONSENT in screen.text for screen in exceptions)


def test_the_consent_screen_still_obeys_every_other_rule() -> None:
    """Снято одно правило, а не проверка целиком."""
    consent = next(screen for screen in texts.SCREENS if screen.formal_address)

    assert check_screen(consent) == []
    assert consent.buttons


def test_buttons_never_get_the_formal_exception() -> None:
    """Послабление касается условий договора, а не разговора с человеком."""
    violations = check_screen(
        Screen(text="Условия", buttons=("Оплатить вашей картой",), formal_address=True)
    )

    assert any(v.rule == "обращение" for v in violations)


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
        "пейволл": scenario_keyboards.paywall(texts.button_invite_for_images(2)),
        "канал": scenario_keyboards.channel_required("https://t.me/channel"),
        "ответ чата": scenario_keyboards.chat_answer(
            mark="0123456789ab", truncated=True, offer_new_dialog=True
        ),
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


def test_the_order_screen_declares_the_change_email_button() -> None:
    """Линтер проверяет только то, что экран о себе объявил.

    Разойтись со сборкой клавиатуры этот список не должен: разошедшись, он
    молча перестанет проверять кнопку, которую человек всё это время видит.
    """
    with_address = texts.payment_order(
        TariffId.PRO,
        days=30,
        amount=599,
        currency=RUB,
        next_charge="30 сентября",
        recurring=True,
        receipt_to="alika@mail.ru",
    )
    without = texts.payment_order(
        TariffId.PRO,
        days=30,
        amount=599,
        currency=RUB,
        next_charge="30 сентября",
        recurring=True,
    )

    assert texts.BUTTON_EMAIL_CHANGE in with_address.buttons
    assert texts.BUTTON_EMAIL_CHANGE not in without.buttons


def test_no_screen_says_razbor() -> None:
    """Ф11-0б: «разбор» — внутреннее имя нормы раздела документов.

    Человек нажимает «Доклад / Реферат», и ни пейволл, ни ошибка не должны
    называть это иначе.
    """
    said = [screen.text.lower() for screen in texts.SCREENS]

    assert not [text for text in said if "разбор" in text]
    assert texts.paywall_documents(renews_on=None).text == (
        "Доклады закончились 😔\nЕщё будут с тарифом — выбери подходящий 👇"
    )
    assert texts.paywall_documents(renews_on="27 сентября").text == (
        "Доклады на этот месяц закончились 😔\n"
        "Новые будут 27 сентября, а можно не ждать:"
    )
    assert texts.document_error().text == (
        "Что-то пошло не так, попробуй ещё раз 🤷 Доклад не потратился."
    )
