"""Тесты лимитов: две корзины, порядок списания, сброс суток."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.limits import (
    Allowance,
    LimitKind,
    Source,
    allowance,
    current_day,
    daily_images,
)
from app.core.models import MessengerKind, TariffId, Usage, User, UserId
from app.core.tariffs import tariff_of

DAY = date(2026, 8, 28)


def make_user(
    *,
    tariff: TariffId = TariffId.FREE,
    bonus_messages: int = 0,
    bonus_images: int = 0,
    bonus_documents: int = 0,
) -> User:
    return User(
        id=UserId(1),
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        tariff=tariff,
        referral_code="code",
        support_number=123456,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        bonus_messages=bonus_messages,
        bonus_images=bonus_images,
        bonus_documents=bonus_documents,
    )


def messages(user: User, used: int) -> Allowance:
    return allowance(
        user,
        Usage(day=DAY, messages_used=used),
        tariff_of(user.tariff),
        LimitKind.MESSAGES,
    )


def images(user: User, used: int) -> Allowance:
    return allowance(
        user,
        Usage(day=DAY, images_used=used),
        tariff_of(user.tariff),
        LimitKind.IMAGES,
    )


def documents(user: User, used: int) -> Allowance:
    return allowance(
        user,
        Usage(day=DAY, documents_used=used),
        tariff_of(user.tariff),
        LimitKind.DOCUMENTS,
    )


# --- Дневная квота -------------------------------------------------------


def test_fresh_free_user_has_the_full_daily_quota() -> None:
    """Сообщения дневные и на бесплатном тарифе. Картинки — нет."""
    user = make_user()

    assert messages(user, 0).daily_left == 20
    assert images(user, 0).daily_left == 0


def test_used_quota_is_subtracted() -> None:
    user = make_user()

    assert messages(user, 12).daily_left == 8


def test_quota_never_goes_negative() -> None:
    """Платная подписка кончилась, израсходовано больше новой квоты.

    Это не долг, это просто ноль: иначе человек ушёл бы в минус и не смог
    писать даже завтра.
    """
    user = make_user()

    assert messages(user, 500).daily_left == 0
    assert messages(user, 500).exhausted is True


# --- Бонусная корзина ----------------------------------------------------


def test_bonus_adds_to_the_total() -> None:
    user = make_user(tariff=TariffId.LITE, bonus_images=5)

    assert images(user, 0).total_left == 45


def test_daily_quota_is_spent_before_the_bonus() -> None:
    """Подарок за друга должен ощущаться как продолжение работы, а не
    растворяться в первый же день.

    Проверяется на платном тарифе: только там есть чему тратиться раньше
    бонуса. На бесплатном дневной корзины нет вовсе.
    """
    user = make_user(tariff=TariffId.LITE, bonus_images=5)

    assert images(user, 0).next_source is Source.DAILY


def test_bonus_kicks_in_when_the_daily_quota_runs_out() -> None:
    user = make_user(tariff=TariffId.LITE, bonus_images=5)

    assert images(user, 40).next_source is Source.BONUS
    assert images(user, 40).exhausted is False


def test_a_free_user_spends_straight_from_the_bonus() -> None:
    """Дневной корзины у него нет, и списывать больше неоткуда."""
    user = make_user(bonus_images=3)

    assert images(user, 0).next_source is Source.BONUS


def test_nothing_left_when_both_baskets_are_empty() -> None:
    user = make_user(bonus_images=0)

    exhausted = images(user, 3)
    assert exhausted.next_source is None
    assert exhausted.exhausted is True


def test_bonus_alone_is_enough_to_keep_working() -> None:
    """Пейволл показывается только когда пусты обе корзины (§2.5)."""
    user = make_user(bonus_messages=50)

    assert messages(user, 20).exhausted is False


# --- Норма картинок ------------------------------------------------------


def test_free_tariff_has_no_daily_images_at_all() -> None:
    """Бесплатные картинки выдаются разово, а не каждый день.

    Ноль в дневной норме — не мелочь: он один отвечает за то, что три
    подаренные при регистрации картинки не превращаются в три в сутки.
    """
    user = make_user(bonus_images=3)

    assert images(user, 0).daily_left == 0
    assert images(user, 0).total_left == 3


def test_a_free_user_spends_the_signup_grant_and_it_does_not_come_back() -> None:
    """Потратив выданное, человек упирается в пейволл, а не ждёт завтра."""
    user = make_user(bonus_images=0)

    assert images(user, 0).exhausted is True


@pytest.mark.parametrize(
    ("tariff", "expected_messages", "expected_images"),
    [
        (TariffId.FREE, 20, 0),
        (TariffId.LITE, 100, 40),
        (TariffId.PRO, 100, 60),
        (TariffId.MAX, 200, 150),
    ],
)
def test_tariff_limits_match_the_brief(
    tariff: TariffId, expected_messages: int, expected_images: int
) -> None:
    """§2.8: числа тарифов взяты из задания. У бесплатного картинок нет."""
    user = make_user(tariff=tariff)

    assert messages(user, 0).daily_left == expected_messages
    assert daily_images(tariff_of(tariff)) == expected_images


# --- Сутки ---------------------------------------------------------------


def test_day_is_counted_in_the_configured_timezone() -> None:
    """22:00 UTC — это уже следующий день в Москве."""
    late_evening_utc = datetime(2026, 8, 28, 22, 0, tzinfo=UTC)

    assert current_day(late_evening_utc, "Europe/Moscow") == date(2026, 8, 29)
    assert current_day(late_evening_utc, "UTC") == date(2026, 8, 28)


def test_day_rolls_over_at_local_midnight() -> None:
    """«Завтра» из текста пейволла должно совпадать с «завтра» у человека."""
    msk = ZoneInfo("Europe/Moscow")
    before = datetime(2026, 8, 28, 23, 59, tzinfo=msk)
    after = datetime(2026, 8, 29, 0, 1, tzinfo=msk)

    assert current_day(before, "Europe/Moscow") == date(2026, 8, 28)
    assert current_day(after, "Europe/Moscow") == date(2026, 8, 29)


def test_naive_datetime_is_rejected() -> None:
    """Иначе сутки съезжали бы на три часа незаметно."""
    with pytest.raises(ValueError, match="часовым поясом"):
        current_day(datetime(2026, 8, 28, 22, 0), "Europe/Moscow")


# --- Разбор документов ---------------------------------------------------


def test_documents_have_no_daily_quota_on_the_free_tariff() -> None:
    """Разбор длинного файла — самый дорогой запрос: сутками он не возобновляется."""
    assert documents(make_user(), 0).daily_left == 0


def test_documents_have_a_daily_quota_on_paid_tariffs() -> None:
    """Там человек платит именно за неё."""
    assert documents(make_user(tariff=TariffId.LITE), 0).daily_left == 5
    assert documents(make_user(tariff=TariffId.PRO), 0).daily_left == 10
    assert documents(make_user(tariff=TariffId.MAX), 0).daily_left == 30


def test_a_free_user_spends_documents_from_the_bonus() -> None:
    """На бесплатном тарифе разовая выдача — единственный источник."""
    user = make_user(bonus_documents=3)

    left = documents(user, 0)
    assert left.total_left == 3
    assert left.next_source is Source.BONUS


def test_documents_do_not_borrow_from_the_image_basket() -> None:
    """Иначе разбор файла молча съедал бы картинки, за которые заплачено отдельно."""
    user = make_user(bonus_images=9, bonus_documents=0)

    assert documents(user, 0).total_left == 0
    assert images(user, 0).total_left == 9


def test_spent_documents_do_not_touch_the_image_counter() -> None:
    """Расход у них раздельный: в профиле человеку видно, что именно кончилось."""
    usage = Usage(day=DAY, images_used=0, documents_used=4)
    user = make_user(tariff=TariffId.PRO)

    spent = allowance(user, usage, tariff_of(user.tariff), LimitKind.DOCUMENTS)
    untouched = allowance(user, usage, tariff_of(user.tariff), LimitKind.IMAGES)

    assert spent.daily_used == 4
    assert untouched.daily_used == 0


def test_documents_run_out_and_show_the_paywall() -> None:
    assert documents(make_user(), 0).exhausted is True
    assert documents(make_user(bonus_documents=1), 0).exhausted is False
