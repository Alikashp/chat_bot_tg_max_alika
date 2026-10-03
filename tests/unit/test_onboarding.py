"""Онбординг и рефералка — критерии приёмки №1 и №9.

Рефералка написана целиком здесь, а не отложена до фазы 6, потому что
онбординг без разбора ref_-ссылки был бы наполовину сделанным сценарием:
ветка deeplink есть, а что она делает — непонятно. Фазе 6 остаётся проводка
в адаптеры и сквозные проверки.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.limits import LimitKind
from app.core.models import Chat, MessengerKind, User
from app.core.scenarios import onboarding, spending
from app.core.scenarios.deps import Deps, Session
from tests.fakes import FakeLogger, FakeMessenger, FrozenClock

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="100")
NEW_USER = "100"


async def start(deps: Deps, payload: str = "", external_id: str = NEW_USER) -> Session:
    return await onboarding.start(
        deps, CHAT, MessengerKind.TELEGRAM, external_id, payload
    )


async def refresh(storage: InMemoryStorage, user: User) -> User:
    fresh = await storage.get_user_by_id(user.id)
    assert fresh is not None
    return fresh


# --- Первый экран (§2.1) -------------------------------------------------


async def test_onboarding_is_two_lines_verbatim(
    deps: Deps, messenger: FakeMessenger
) -> None:
    """Критерий приёмки №1. Лимиты с первого экрана убраны заказчиком."""
    await start(deps)

    assert messenger.last_text.text == (
        "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку.\n"
        "Просто напиши мне что-нибудь 👇"
    )


async def test_onboarding_shows_the_menu(deps: Deps, messenger: FakeMessenger) -> None:
    """§2.1: сразу под первым экраном — постоянное меню.

    Ядро просит меню флагом show_menu, а не клавиатурой под сообщением:
    у сообщения в Telegram может быть только одна клавиатура, и передай сюда
    ядро четыре кнопки — постоянное меню превратилось бы в кнопки под одним
    сообщением и исчезло со следующим. Из каких кнопок меню состоит,
    проверяется на адаптере (tests/integration/test_telegram_flow.py).
    """
    await start(deps)

    assert messenger.last_text.show_menu is True
    assert messenger.last_text.keyboard is None


async def test_new_user_gets_a_referral_code(
    deps: Deps, storage: InMemoryStorage
) -> None:
    session = await start(deps)

    assert session.user.referral_code
    found = await storage.get_user_by_referral_code(session.user.referral_code)
    assert found is not None


# --- Ветка бота презентаций (§2.1) ---------------------------------------


async def test_presentation_deeplink_replaces_the_first_line(
    deps: Deps, messenger: FakeMessenger
) -> None:
    await start(deps, payload="pres_autumn")

    assert messenger.last_text.text.split("\n")[0] == (
        "Привет! Ты из бота презентаций — здесь ещё чат и картинки. "
        "Держи бонусные картинки за переход."
    )


async def test_presentation_deeplink_raises_the_signup_grant(
    deps: Deps, messenger: FakeMessenger
) -> None:
    """§2.1: 5 картинок вместо 3.

    Три — месячная норма бесплатного тарифа, она у всех. Ещё две за переход
    ложатся в бонус: он не сгорает вместе с первым периодом. В тексте
    первого экрана их не называют — число проверяется по самой выдаче.
    """
    session = await start(deps, payload="pres_autumn")

    assert session.user.bonus_images == 2
    left = await spending.current_allowance(deps, session, LimitKind.IMAGES)
    assert left.total_left == 5


async def test_a_fresh_user_gets_documents_to_try(deps: Deps) -> None:
    """Разборы выдаются разово, как и картинки: дневной нормы у них нет.

    Без этой выдачи раздел документов на бесплатном тарифе был бы закрыт
    сразу и наглухо — человек не увидел бы, за что ему предлагают платить.
    """
    session = await start(deps)

    assert session.user.bonus_documents == 2


async def test_a_new_person_gets_exactly_the_free_grants(deps: Deps) -> None:
    """Т4: ровно 3 картинки, 1 презентация и 2 доклада — не больше и не меньше.

    Картинки — месячной нормой вместо прежних трёх разовых, а не вместе с
    ними: бонуса картинок у нового человека нет.
    """
    session = await start(deps)

    left = {
        kind: await spending.current_allowance(deps, session, kind)
        for kind in (LimitKind.IMAGES, LimitKind.PRESENTATIONS, LimitKind.DOCUMENTS)
    }
    assert {kind: grant.total_left for kind, grant in left.items()} == {
        LimitKind.IMAGES: 3,
        LimitKind.PRESENTATIONS: 1,
        LimitKind.DOCUMENTS: 2,
    }
    assert left[LimitKind.IMAGES].bonus == 0


async def test_the_document_grant_is_separate_from_the_image_one(
    deps: Deps,
) -> None:
    """Пять картинок из презентаций не должны превращаться в пять разборов."""
    session = await start(deps, payload="pres_autumn")

    assert session.user.bonus_images == 2
    assert session.user.bonus_documents == 2


# --- Источник регистрации ------------------------------------------------


async def test_the_link_a_person_came_by_is_recorded(deps: Deps) -> None:
    session = await start(deps, payload="ppt_result")

    assert session.user.source == "ppt_result"


async def test_a_referral_link_is_recorded_as_it_is(deps: Deps, user: User) -> None:
    """Код внутри источника нужен: по нему видно, чьё приглашение сработало."""
    session = await start(deps, payload=f"ref_{user.referral_code}")

    assert session.user.source == f"ref_{user.referral_code}"


async def test_coming_without_a_link_is_called_by_a_word(deps: Deps) -> None:
    """Пустая ячейка одинаково читается и как «пришёл сам», и как «не записали»."""
    session = await start(deps)

    assert session.user.source == "direct"


async def test_the_source_is_not_rewritten_on_a_later_start(
    deps: Deps, storage: InMemoryStorage
) -> None:
    """Источник отвечает на «откуда он взялся», а не «где был в последний раз».

    Перезапись превратила бы его в бесполезный «последний deeplink», по
    которому нельзя посчитать ни одну кампанию.
    """
    first = await start(deps, payload="catalog")

    await start(deps, payload="ppt_result")

    assert (await refresh(storage, first.user)).source == "catalog"


# --- Рефералка (§2.7) ----------------------------------------------------


async def test_referrer_is_told_immediately(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    await start(deps, payload=f"ref_{user.referral_code}")

    expected = texts.referral_reward(messages=20, images=2).text
    assert expected in messenger.texts_said()


async def test_the_referrer_notice_names_the_person_not_the_chat(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    """Разговор с пригласившим начинаем мы, номера переписки у нас нет.

    В MAX номер человека и номер переписки — разные числа, и без этой
    пометки поздравление уехало бы не туда. Ровно так однажды потерялось
    подтверждение оплаты.
    """
    await start(deps, payload=f"ref_{user.referral_code}")

    notice = next(
        sent
        for sent in messenger.texts
        if sent.text == texts.referral_reward(messages=20, images=2).text
    )
    assert notice.chat.is_person is True
    assert notice.chat.chat_id == user.external_id


async def test_the_reward_goes_to_the_referrer_alone(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Фаза 10, К7: +2 картинки и +20 сообщений — пригласившему, и только ему."""
    session = await start(deps, payload=f"ref_{user.referral_code}")

    referrer = await refresh(storage, user)
    assert (referrer.bonus_messages, referrer.bonus_images) == (20, 3 + 2)
    invited = await refresh(storage, session.user)
    # Подарка от друга нет: бонус у приглашённого пустой.
    assert (invited.bonus_messages, invited.bonus_images) == (0, 0)


async def test_the_invited_user_is_promised_nothing(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    """Приглашённому не дарят — значит, и первый экран о подарке молчит."""
    await start(deps, payload=f"ref_{user.referral_code}")

    assert messenger.last_text.text == texts.onboarding().text
    assert "подар" not in messenger.last_text.text


async def test_referral_is_idempotent(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Критерий приёмки №9: повторный /start не начисляет ничего.

    Гарантия идёт от хранилища, а не от аккуратности этого кода: пара
    (пригласивший, приглашённый) физически не может записаться дважды.
    """
    payload = f"ref_{user.referral_code}"
    await start(deps, payload=payload)
    await start(deps, payload=payload)

    referrer = await refresh(storage, user)
    assert (referrer.bonus_messages, referrer.bonus_images) == (20, 5)
    assert await storage.count_referrals(user.id) == 1


async def test_self_referral_earns_nothing(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Критерий приёмки №9: ссылка на самого себя заблокирована."""
    await onboarding.start(
        deps,
        Chat(messenger=MessengerKind.TELEGRAM, chat_id=user.external_id),
        MessengerKind.TELEGRAM,
        user.external_id,
        f"ref_{user.referral_code}",
    )

    fresh = await refresh(storage, user)
    # Три картинки — выданные при регистрации, а не награда за себя самого.
    assert (fresh.bonus_messages, fresh.bonus_images) == (0, 3)
    assert await storage.count_referrals(user.id) == 0


async def test_unknown_code_earns_nothing_but_still_greets(
    deps: Deps, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    session = await start(deps, payload="ref_нетакого")

    fresh = await refresh(storage, session.user)
    assert (fresh.bonus_messages, fresh.bonus_images) == (0, 0)
    assert "подарок" not in messenger.last_text.text


async def test_existing_user_earns_nothing_on_a_second_start(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """§2.7: награда только за нового пользователя."""
    other = await storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="200",
        referral_code="code200",
        support_number=support.generate_number(),
        bonus_images=3,
        bonus_documents=0,
    )

    await start(deps, payload=f"ref_{other.referral_code}", external_id="1")

    # Только то, что выдано при регистрации: награды за знакомого не было.
    assert (await refresh(storage, other)).bonus_images == 3


def capped(deps: Deps, limit: int) -> Deps:
    """Те же зависимости, но с включённым суточным потолком наград."""
    return replace(
        deps, settings=replace(deps.settings, referral_daily_reward_limit=limit)
    )


async def test_by_default_twenty_friends_a_day_are_rewarded(
    deps: Deps, storage: InMemoryStorage, user: User, logger: FakeLogger
) -> None:
    """Фаза 10, К7: потолок наград по умолчанию — двадцать друзей в сутки.

    Награда за друга теперь включает презентацию, а она стоит денег. Двадцать
    живых друзей за сутки — с запасом для честного человека и стена для
    того, кто регистрирует друзей скриптом.
    """
    payload = f"ref_{user.referral_code}"
    for index in range(25):
        await start(deps, payload=payload, external_id=f"guest{index}")

    # Три при регистрации плюс по две за каждого из первых двадцати друзей.
    assert (await refresh(storage, user)).bonus_images == 3 + 20 * 2
    assert "referral_limit_reached" in logger.names()


async def test_daily_reward_limit_stops_farming_when_switched_on(
    deps: Deps, storage: InMemoryStorage, user: User, logger: FakeLogger
) -> None:
    """§2.7: потолок наград в сутки, если его всё-таки включили."""
    limited = capped(deps, 2)
    payload = f"ref_{user.referral_code}"
    for index in range(2):
        await start(limited, payload=payload, external_id=f"guest{index}")

    before = (await refresh(storage, user)).bonus_images
    await start(limited, payload=payload, external_id="guest-over-the-limit")

    assert (await refresh(storage, user)).bonus_images == before
    assert "referral_limit_reached" in logger.names()


async def test_reward_limit_resets_with_the_day(
    deps: Deps, storage: InMemoryStorage, user: User, clock: FrozenClock
) -> None:
    """Потолок суточный, а не пожизненный."""
    limited = capped(deps, 2)
    payload = f"ref_{user.referral_code}"
    for index in range(2):
        await start(limited, payload=payload, external_id=f"guest{index}")
    before = (await refresh(storage, user)).bonus_images

    clock.advance(days=1, minutes=1)
    await start(limited, payload=payload, external_id="guest-tomorrow")

    assert (await refresh(storage, user)).bonus_images > before


async def test_a_failure_to_notify_does_not_undo_the_reward(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Бонус уже начислен — ронять из-за этого онбординг приглашённому нельзя."""
    messenger.fail_send = RuntimeError("не доставлено")

    with contextlib.suppress(RuntimeError):
        await start(deps, payload=f"ref_{user.referral_code}")

    assert (await refresh(storage, user)).bonus_images == 5  # 3 при входе + 2
