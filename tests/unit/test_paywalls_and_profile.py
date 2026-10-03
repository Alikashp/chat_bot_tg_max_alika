"""Пейволлы и профиль при месячной норме (фаза 11, часть 2, Т7 и Т8).

Пейволл обязан сказать, когда норма обновится, — и ровно то, что правда
будет: бесплатному тарифу докладов не обещать, а платному без продления не
обещать новой платной нормы. И с каждого пейволла должен быть выход.

Профиль показывает остаток как норму плюс бонус.
"""

from __future__ import annotations

from dataclasses import replace

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.limits import LimitKind
from app.core.models import Chat, TariffId, User
from app.core.scenarios import payments, paywall, profile, spending, subscriptions
from app.core.scenarios.deps import Deps, Session
from tests.fakes import (
    FakeMessenger,
    FakePresentations,
    FakeStars,
    FrozenClock,
    use_up_norm,
)

#: Конец первого периода и для бесплатного (регистрация 28 августа), и для
#: платного (оплата 28 августа): тридцать дней спустя.
MONTH_END = "27 сентября"


async def _fresh(deps: Deps, user: User) -> Session:
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None
    return Session(
        user=found,
        chat=Chat(messenger=found.messenger, chat_id=found.external_id),
        day=deps.today(),
        now=deps.now(),
    )


async def _pro(deps: Deps, session: Session, stars: FakeStars) -> Session:
    """Человек с подпиской «Про» на звёздах — продление включено."""
    await payments.start_stars(deps, session, TariffId.PRO)
    await payments.confirm(deps, stars.invoices[-1].order_id, charge_id="charge-1")
    return await _fresh(deps, session.user)


async def _empty(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Ни нормы, ни бонуса этого вида."""
    await use_up_norm(deps, session, kind)
    user = await deps.storage.get_user_by_id(session.user.id)
    assert user is not None
    bonus = {
        LimitKind.IMAGES: user.bonus_images,
        LimitKind.DOCUMENTS: user.bonus_documents,
        LimitKind.PRESENTATIONS: user.bonus_presentations,
    }[kind]
    assert await deps.storage.spend_bonus(user.id, **{kind.value: bonus}) or bonus == 0


def _buttons(messenger: FakeMessenger) -> list[str]:
    keyboard = messenger.last_text.keyboard
    assert keyboard is not None, "пейволл без выхода"
    return [button.text for row in keyboard.rows for button in row]


# --- Т7: пейволлы ---------------------------------------------------------


async def test_a_free_person_out_of_images_is_told_when_new_ones_come(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await _empty(deps, session, LimitKind.IMAGES)

    await paywall.show(deps, session, LimitKind.IMAGES)

    assert messenger.last_text.text == (
        "Картинки на этот месяц закончились 😔\n"
        f"Новые будут {MONTH_END}, а можно не ждать:"
    )
    assert texts.BUTTON_OPEN_TARIFFS in _buttons(messenger)


async def test_a_free_person_out_of_reports_is_not_promised_new_ones(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """У бесплатного тарифа докладов в месяц нет — и обещать их нечего."""
    await _empty(deps, session, LimitKind.DOCUMENTS)

    await paywall.show(deps, session, LimitKind.DOCUMENTS)

    assert messenger.last_text.text == (
        "Доклады закончились 😔\nЕщё будут с тарифом — выбери подходящий 👇"
    )
    assert MONTH_END not in messenger.last_text.text
    assert _buttons(messenger) == [texts.BUTTON_OPEN_TARIFFS]


async def test_the_presentations_paywall_offers_tariffs(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    """Т7: презентации вошли в тарифы — и на их пейволле появилась кнопка."""
    on = replace(deps, presentations=FakePresentations())
    await _empty(on, session, LimitKind.PRESENTATIONS)

    await paywall.show(on, session, LimitKind.PRESENTATIONS)

    assert messenger.last_text.text == (
        "Презентации закончились 😔\nЕщё будут с тарифом или за друга:"
    )
    assert _buttons(messenger) == [
        texts.BUTTON_OPEN_TARIFFS,
        texts.button_invite_for_presentations(1),
    ]


async def test_a_renewing_subscriber_is_told_the_norm_comes_with_the_renewal(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    """Новая норма докладов придёт только с продлением — так и сказано."""
    pro = await _pro(deps, session, stars)
    await _empty(deps, pro, LimitKind.DOCUMENTS)

    await paywall.show(deps, pro, LimitKind.DOCUMENTS)

    assert messenger.last_text.text == (
        "Доклады на этот месяц закончились 😔\n"
        f"Новые придут с продлением {MONTH_END}, а можно не ждать:"
    )


async def test_after_cancelling_no_new_paid_norm_is_promised(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    """Продление отключено: после срока — бесплатный тариф, докладов в нём нет."""
    pro = await _pro(deps, session, stars)
    await subscriptions.cancel(deps, pro)
    await _empty(deps, pro, LimitKind.DOCUMENTS)

    await paywall.show(deps, pro, LimitKind.DOCUMENTS)

    assert MONTH_END not in messenger.last_text.text
    assert messenger.last_text.text.startswith("Доклады закончились")


async def test_after_cancelling_free_images_still_come(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    """Картинки есть и в бесплатном тарифе — значит, новые правда будут."""
    pro = await _pro(deps, session, stars)
    await subscriptions.cancel(deps, pro)
    await _empty(deps, pro, LimitKind.IMAGES)

    await paywall.show(deps, pro, LimitKind.IMAGES)

    assert messenger.last_text.text == (
        "Картинки на этот месяц закончились 😔\n"
        f"Новые будут {MONTH_END}, а можно не ждать:"
    )


async def test_a_prepaid_month_renews_without_any_charge(
    deps: Deps,
    session: Session,
    stars: FakeStars,
    messenger: FakeMessenger,
    clock: FrozenClock,
    storage: InMemoryStorage,
) -> None:
    """Оплачено на два месяца вперёд — второй месяц не зависит от продления."""
    pro = await _pro(deps, session, stars)
    clock.advance(hours=1)
    await payments.start_stars(deps, pro, TariffId.PRO)
    await payments.confirm(deps, stars.invoices[-1].order_id, charge_id="charge-2")
    await storage.cancel_subscription(pro.user.id, deps.now())
    prepaid = await _fresh(deps, pro.user)
    await _empty(deps, prepaid, LimitKind.DOCUMENTS)

    await paywall.show(deps, prepaid, LimitKind.DOCUMENTS)

    assert messenger.last_text.text.split("\n")[1].startswith("Новые будут ")


async def test_messages_still_come_tomorrow(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Сообщения — дневная норма: «завтра будут ещё» здесь по-прежнему правда."""
    await storage.add_usage(session.user.id, session.day, messages=20)

    await paywall.show(deps, session, LimitKind.MESSAGES)

    assert "Завтра будут ещё" in messenger.last_text.text


def test_no_monthly_paywall_promises_tomorrow() -> None:
    """Т7: «на сегодня» и «завтра» остались только у сообщений."""
    screens = [
        texts.paywall_images(renews_on=MONTH_END, invite_images=2),
        texts.paywall_images(renews_on=None, invite_images=2),
        texts.paywall_documents(renews_on=MONTH_END),
        texts.paywall_documents(renews_on=MONTH_END, by_charge=True),
        texts.paywall_documents(renews_on=None),
        texts.paywall_presentations(1, renews_on=MONTH_END),
        texts.paywall_presentations(1, renews_on=None),
    ]

    for screen in screens:
        assert "сегодня" not in screen.text
        assert "завтра" not in screen.text.lower()
        assert texts.BUTTON_OPEN_TARIFFS in screen.buttons


# --- Т8: профиль ----------------------------------------------------------


async def test_a_new_free_profile_shows_one_number_per_resource(
    deps: Deps, session: Session, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """0б: остаток — одно число, норма плюс подарки; ни «+», ни 🎁."""
    await storage.add_bonus(session.user.id, documents=2)

    await profile.show(deps, session)

    assert messenger.last_text.text == (
        f"Твой тариф: Бесплатный · новые картинки {MONTH_END}\n"
        "Сообщений сегодня: 0 из 20\n"
        "Картинки: 6 · Доклад / Реферат: 2\n"
        "Друзей позвал: 0"
    )


async def test_a_paid_profile_counts_what_is_left_of_each_norm(
    deps: Deps,
    session: Session,
    stars: FakeStars,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
) -> None:
    on = replace(deps, presentations=FakePresentations())
    pro = await _pro(on, session, stars)
    for _ in range(5):
        await spending.charge(on, pro, LimitKind.IMAGES)
    await spending.charge(on, pro, LimitKind.PRESENTATIONS)
    await storage.add_bonus(pro.user.id, presentations=1)

    await profile.show(on, pro)

    lines = messenger.last_text.text.split("\n")
    assert lines[0] == f"Твой тариф: Про · новый месяц с {MONTH_END}"
    assert lines[2] == ("Картинки: 38 · Доклад / Реферат: 40 · Презентации: 25")


async def test_a_profile_without_renewal_says_when_the_tariff_ends(
    deps: Deps, session: Session, stars: FakeStars, messenger: FakeMessenger
) -> None:
    pro = await _pro(deps, session, stars)
    await subscriptions.cancel(deps, pro)

    await profile.show(deps, pro)

    assert messenger.last_text.text.split("\n")[0] == (
        f"Твой тариф: Про · до {MONTH_END}"
    )


async def test_the_profile_shows_zero_when_nothing_is_left(
    deps: Deps, session: Session, messenger: FakeMessenger
) -> None:
    await _empty(deps, session, LimitKind.IMAGES)

    await profile.show(deps, session)

    assert "Картинки: 0 · " in messenger.last_text.text
