"""Человек с действующим пакетом — платящий везде (сессия 17, шаг 2, К2).

Перечень мест, где бот различает бесплатный и платный тариф (поиск по
`is_free`, `TariffId.FREE`, `active_tariff` и признакам тарифа):

1. приколы с замком — `presets._is_locked`;
2. обязательная подписка на канал — `channel.active`;
3. рассылка реактивации R1–R3 и подарок R2 — `mailing._eligible` и выборка
   `Storage.mailing_candidates` (контракт — в tests/contract);
4. бесплатная перегенерация первой презентации — `presentations`;
5. пейволл: бесплатному — предложение пакета, платящему — свой (`paywall`);
6. модель ответов — `Session.model`;
7. профиль — «Твой пакет» или «Твой тариф: Бесплатный»;
8. экран заказа — «остатки сложатся» только у действующего;
9. нормы и их период — `limits.norm_period`, `effective_tariff`
   (tests/unit/test_packages.py, сессия 16).

Каждое место проверено дважды: с действующим пакетом — как у платящего, и
после конца пакета — снова как у бесплатного. Качество картинок у всех
тарифов одно (medium), поэтому различать там нечего.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.actions import Action, buy_action
from app.core.limits import LimitKind
from app.core.mailing import Mailer
from app.core.models import Chat, IncomingMessage, MessengerKind, TariffId, User
from app.core.router import handle
from app.core.scenarios import paywall, presets, spending
from app.core.scenarios.deps import Deps, session_for
from config.presets import PRESETS, Preset
from tests.fakes import (
    FakeChannel,
    FakeLLM,
    FakeMessenger,
    FakePresentations,
    FakeStars,
    FrozenClock,
)

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1", person="1")
CHANNEL = "https://t.me/chatgptbotonline"
TOPIC = "Как работает фотосинтез"
REGENERATE = "🔄 Перегенерировать"

#: С действующим пакетом — платящий; после конца — снова бесплатный.
STATES = ["package", "ended"]


@pytest.fixture
def telegram(deps: Deps) -> Deps:
    return replace(
        deps,
        cards=None,
        settings=replace(
            deps.settings, card_sales_enabled=False, packages_enabled=True
        ),
    )


async def put_on(
    deps: Deps, user: User, state: str, package: TariffId = TariffId.WEEK
) -> User:
    """Пакет, который действует ещё неделю, — или кончился минуту назад."""
    ends = (
        deps.now() + timedelta(days=7)
        if state == "package"
        else deps.now() - timedelta(minutes=1)
    )
    await deps.storage.set_tariff(user.id, package, ends)
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None
    return found


def press(action: str) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", action=action)


def labels(messenger: FakeMessenger) -> list[str]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    return [button.text for row in keyboard.rows for button in row]


# --- 1. Приколы с замком -----------------------------------------------------


@pytest.fixture
def locked() -> Preset:
    return next(
        preset
        for preset in PRESETS.values()
        if preset.paid_only and preset.photos_required == 1
    )


@pytest.mark.parametrize("state", STATES)
async def test_locked_presets_open_on_a_package(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    locked: Preset,
    state: str,
) -> None:
    found = await put_on(telegram, user, state)
    session = session_for(telegram, found)

    await presets.show_menu(telegram, session)
    menu = labels(messenger)
    await presets.pick(telegram, session, locked)

    if state == "package":
        assert locked.button in menu
        assert messenger.last_text.text != texts.PRESET_LOCKED
    else:
        assert texts.locked_button(locked.button) in menu
        assert messenger.last_text.text == texts.PRESET_LOCKED


# --- 2. Обязательная подписка на канал ---------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_the_channel_check_does_not_stop_a_package(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    llm: FakeLLM,
    channel_: FakeChannel,
    state: str,
) -> None:
    gated = replace(
        telegram,
        settings=replace(telegram.settings, channel_url=CHANNEL, channel_required=True),
    )
    await put_on(gated, user, state)
    llm.answer = "Ответ."

    await handle(gated, IncomingMessage(chat=CHAT, external_user_id="1", text="привет"))

    if state == "package":
        assert messenger.last_text.text == "Ответ."
        assert channel_.asked == []
    else:
        assert messenger.last_text.text == texts.CHANNEL_REQUIRED


# --- 3. Рассылка реактивации R1–R3 и подарок R2 --------------------------------


@pytest.fixture
def mailing_on(telegram: Deps) -> Deps:
    return replace(
        telegram,
        presentations=FakePresentations(),
        settings=replace(
            telegram.settings,
            mailings_enabled=True,
            mailing_r1_enabled=True,
            mailing_r2_enabled=True,
            mailing_r3_enabled=True,
        ),
    )


async def quiet(storage: InMemoryStorage) -> User:
    return await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="7",
        referral_code="code7",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )


async def test_a_quiet_person_on_a_package_gets_no_mailing_all_month(
    mailing_on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """Полгода, человек молчит месяц: ни R1, ни R2 с подарком, ни R3."""
    user = await quiet(storage)
    await storage.set_tariff(
        user.id, TariffId.HALFYEAR, mailing_on.now() + timedelta(days=150)
    )

    for _ in range(30 * 24):
        await Mailer(deps=mailing_on).run()
        clock.advance(hours=1)

    assert messenger.texts == []
    assert await storage.mailings_of(user.id) == []
    assert await storage.count_gifts(user.id, LimitKind.PRESENTATIONS, clock()) == 0


@pytest.mark.parametrize("state", STATES)
async def test_r1_goes_only_after_the_package_ended(
    mailing_on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    state: str,
) -> None:
    """Неделя куплена, человек замолчал на четверо суток. Пакет ещё идёт —
    R1 нет; пакет кончился за час до того — R1 уходит, как бесплатному."""
    user = await quiet(storage)
    lasts = timedelta(days=7) if state == "package" else timedelta(days=4)
    await storage.set_tariff(user.id, TariffId.WEEK, mailing_on.now() + lasts)
    clock.advance(days=4, hours=1)

    await Mailer(deps=mailing_on).run()

    sent = [each.step for each in await storage.mailings_of(user.id)]
    assert sent == ([] if state == "package" else ["R1"])


# --- 4. Бесплатная перегенерация презентации ------------------------------------


def button(messenger: FakeMessenger, label: str) -> str:
    for sent in reversed(messenger.texts):
        if sent.keyboard is None:
            continue
        for row in sent.keyboard.rows:
            for found in row:
                if found.text == label and found.action:
                    return found.action
    raise AssertionError(f"нет кнопки «{label}»")


async def build_once(deps: Deps, messenger: FakeMessenger) -> None:
    def incoming(**fields: object) -> IncomingMessage:
        return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]

    await handle(deps, incoming(action=Action.MENU_PRESENTATIONS))
    await handle(deps, incoming(text=TOPIC))
    await handle(deps, incoming(action=button(messenger, texts.BUTTON_DECK_BUILD)))


async def presentations_left(deps: Deps, user: User) -> int:
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None
    allowance = await spending.current_allowance(
        deps, session_for(deps, found), LimitKind.PRESENTATIONS
    )
    return allowance.total_left


@pytest.mark.parametrize("state", STATES)
async def test_the_free_regeneration_is_a_free_tariff_rule(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    state: str,
) -> None:
    """На пакете первая презентация перегенерируется за счёт пакета, как у
    платящего; после конца пакета — первая презентация бесплатного, и её
    перегенерация бесплатна один раз."""
    decks = FakePresentations()
    enabled = replace(telegram, presentations=decks)
    await put_on(enabled, user, state)
    # Разовая презентация бесплатного — та, что даётся при регистрации.
    await storage.add_bonus(user.id, presentations=1)
    before = await presentations_left(enabled, user)

    await build_once(enabled, messenger)
    await handle(enabled, press(button(messenger, REGENERATE)))

    assert len(decks.requests) == 2
    spent = before - await presentations_left(enabled, user)
    assert spent == (2 if state == "package" else 1)


# --- 5. Пейволл ------------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_the_paywall_is_the_paying_one_on_a_package(
    telegram: Deps, user: User, messenger: FakeMessenger, state: str
) -> None:
    found = await put_on(telegram, user, state)

    await paywall.show(telegram, session_for(telegram, found), LimitKind.IMAGES)

    text = messenger.last_text.text
    if state == "package":
        assert text.startswith("Картинки пакета закончились")
    else:
        assert "Открой всё на 7 дней за 149 ⭐:" in text


# --- 6. Модель ответов ------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_the_half_year_answers_with_the_standard_model(
    telegram: Deps, user: User, state: str
) -> None:
    found = await put_on(telegram, user, state, TariffId.HALFYEAR)

    model = session_for(telegram, found).model(telegram.settings)

    expected = (
        telegram.settings.model_standard
        if state == "package"
        else telegram.settings.model_economy
    )
    assert model == expected


# --- 7. Профиль -------------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_the_profile_tells_a_package_from_the_free_tariff(
    telegram: Deps, user: User, messenger: FakeMessenger, state: str
) -> None:
    await put_on(telegram, user, state)

    await handle(telegram, press(Action.MENU_PROFILE))

    expected = "Твой пакет: Неделя" if state == "package" else "Твой тариф: Бесплатный"
    assert messenger.last_text.text.startswith(expected)


# --- 8. Экран заказа ----------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_only_a_live_package_promises_adding_up(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    stars: FakeStars,
    state: str,
) -> None:
    await put_on(telegram, user, state)

    await handle(telegram, press(buy_action("month")))

    promise = "Остатки нынешнего пакета сложатся с новым."
    assert (promise in messenger.last_text.text) is (state == "package")


async def test_the_core_rechecks_a_package_even_if_the_base_offers_it(
    mailing_on: Deps,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
) -> None:
    """База могла отдать устаревший список (пакет куплен, пока шёл проход) —
    ядро проверяет тариф каждого заново, и пакет для него платный."""
    user = await quiet(storage)
    clock.advance(days=4, hours=1)
    await storage.set_tariff(user.id, TariffId.WEEK, clock.now + timedelta(days=7))

    async def everyone(**_: object) -> list[User]:
        found = await storage.get_user_by_id(user.id)
        assert found is not None
        return [found]

    storage.mailing_candidates = everyone  # type: ignore[method-assign]

    await Mailer(deps=mailing_on, batch=1).run()

    assert messenger.texts == []


@pytest.mark.parametrize(
    ("built_on", "regenerated_on", "spent"),
    [("free", "package", 2), ("package", "free", 2)],
    ids=["built-free-regenerated-on-package", "built-on-package-after-it-ended"],
)
async def test_the_free_regeneration_follows_the_tariff_on_both_ends(
    telegram: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    clock: FrozenClock,
    built_on: str,
    regenerated_on: str,
    spent: int,
) -> None:
    """Правило бесплатного действует, только когда тариф бесплатный и при
    сборке, и при перегенерации. Собрал первую на бесплатном, купил пакет —
    перегенерация за счёт пакета; собрал на пакете, пакет кончился — первая
    колода была не бесплатной, и даром её не перегенерировать."""
    decks = FakePresentations()
    enabled = replace(telegram, presentations=decks)
    await storage.add_bonus(user.id, presentations=2)
    if built_on == "package":
        await storage.set_tariff(
            user.id, TariffId.WEEK, enabled.now() + timedelta(hours=1)
        )
    before_build = await presentations_left(enabled, user)
    await build_once(enabled, messenger)
    built = before_build - await presentations_left(enabled, user)

    if regenerated_on == "package":
        await storage.set_tariff(
            user.id, TariffId.WEEK, enabled.now() + timedelta(days=7)
        )
    else:
        # Кнопка перегенерации живёт часы, а не сутки: пакет кончился раньше.
        clock.advance(hours=1, minutes=1)
    before = await presentations_left(enabled, user)
    await handle(enabled, press(button(messenger, REGENERATE)))

    assert len(decks.requests) == 2
    assert built == 1
    assert before - await presentations_left(enabled, user) == spent - built
