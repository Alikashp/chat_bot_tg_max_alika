"""Онбординг и рефералка — критерии приёмки №1 и №9.

Рефералка написана целиком здесь, а не отложена до фазы 6, потому что
онбординг без разбора ref_-ссылки был бы наполовину сделанным сценарием:
ветка deeplink есть, а что она делает — непонятно. Фазе 6 остаётся проводка
в адаптеры и сквозные проверки.
"""

from __future__ import annotations

import contextlib

from app.adapters.storage.memory import InMemoryStorage
from app.core import support, texts
from app.core.models import Chat, MessengerKind, User
from app.core.scenarios import onboarding
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


async def test_onboarding_is_three_lines_verbatim(
    deps: Deps, messenger: FakeMessenger
) -> None:
    """Критерий приёмки №1."""
    await start(deps)

    assert messenger.last_text.text == (
        "Привет! Я отвечу на любой вопрос, решу задачу и сделаю картинку.\n"
        "Просто напиши мне что-нибудь 👇\n"
        "У тебя 20 сообщений в день и 3 картинки бесплатно."
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
        "Держи 5 картинок вместо 3 за переход."
    )


async def test_presentation_deeplink_raises_the_image_quota(
    deps: Deps, messenger: FakeMessenger
) -> None:
    """§2.1: 5 картинок вместо 3 — и это видно в самом тексте."""
    session = await start(deps, payload="pres_autumn")

    assert session.user.daily_image_quota == 5
    assert "5 картинок бесплатно" in messenger.last_text.text


# --- Рефералка (§2.7) ----------------------------------------------------


async def test_referral_rewards_both_sides(
    deps: Deps, storage: InMemoryStorage, user: User
) -> None:
    """Критерий приёмки №9: награда обоим и сразу."""
    session = await start(deps, payload=f"ref_{user.referral_code}")

    referrer = await refresh(storage, user)
    referee = await refresh(storage, session.user)
    assert (referrer.bonus_messages, referrer.bonus_images) == (50, 5)
    assert (referee.bonus_messages, referee.bonus_images) == (50, 5)


async def test_referrer_is_told_immediately(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    await start(deps, payload=f"ref_{user.referral_code}")

    assert texts.REFERRAL_REWARD in messenger.texts_said()


async def test_invited_user_sees_the_gift_in_onboarding(
    deps: Deps, messenger: FakeMessenger, user: User
) -> None:
    await start(deps, payload=f"ref_{user.referral_code}")

    assert messenger.last_text.text.endswith(
        "Тебе подарок от друга: +50 сообщений и +5 картинок."
    )


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
    assert (referrer.bonus_messages, referrer.bonus_images) == (50, 5)
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
    assert (fresh.bonus_messages, fresh.bonus_images) == (0, 0)
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
        daily_image_quota=3,
    )

    await start(deps, payload=f"ref_{other.referral_code}", external_id="1")

    assert (await refresh(storage, other)).bonus_images == 0


async def test_daily_reward_limit_stops_farming(
    deps: Deps, storage: InMemoryStorage, user: User, logger: FakeLogger
) -> None:
    """§2.7: потолок наград в сутки на одного пригласившего."""
    payload = f"ref_{user.referral_code}"
    for index in range(deps.settings.referral_daily_reward_limit):
        await start(deps, payload=payload, external_id=f"guest{index}")

    before = (await refresh(storage, user)).bonus_images
    await start(deps, payload=payload, external_id="guest-over-the-limit")

    assert (await refresh(storage, user)).bonus_images == before
    assert "referral_limit_reached" in logger.names()


async def test_reward_limit_resets_with_the_day(
    deps: Deps, storage: InMemoryStorage, user: User, clock: FrozenClock
) -> None:
    """Потолок суточный, а не пожизненный."""
    payload = f"ref_{user.referral_code}"
    for index in range(deps.settings.referral_daily_reward_limit):
        await start(deps, payload=payload, external_id=f"guest{index}")
    before = (await refresh(storage, user)).bonus_images

    clock.advance(days=1, minutes=1)
    await start(deps, payload=payload, external_id="guest-tomorrow")

    assert (await refresh(storage, user)).bonus_images > before


async def test_a_failure_to_notify_does_not_undo_the_reward(
    deps: Deps, storage: InMemoryStorage, user: User, messenger: FakeMessenger
) -> None:
    """Бонус уже начислен — ронять из-за этого онбординг приглашённому нельзя."""
    messenger.fail_send = RuntimeError("не доставлено")

    with contextlib.suppress(RuntimeError):
        await start(deps, payload=f"ref_{user.referral_code}")

    assert (await refresh(storage, user)).bonus_images == 5
