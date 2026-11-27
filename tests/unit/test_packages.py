"""Разовые пакеты Telegram (сессия 16, шаг 1, П1, часть П4).

Пакеты заданы в одном месте — в реестре тарифов, рядом с Лайтом, Про и
Максом. Цена — в звёздах сразу, а не через рублёвую формулу. Картинки,
презентации и доклады — на весь срок пакета и внутри него не обновляются;
сообщения — дневная норма. После конца срока — бесплатные нормы.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core import support
from app.core.limits import LimitKind, allowance, norm_period
from app.core.models import MessengerKind, PeriodUsage, TariffId, Usage, User
from app.core.tariffs import (
    PACKAGES,
    PAID_TARIFFS,
    TARIFFS,
    active_tariff,
    tariff_of,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)

#: Таблица из поручения: цена, срок, сообщения в день, картинки,
#: презентации, доклады.
TABLE = {
    TariffId.WEEK: (149, 7, 100, 5, 3, 5),
    TariffId.MONTH: (349, 30, 100, 20, 10, 15),
    TariffId.HALFYEAR: (1490, 180, 200, 120, 60, 90),
}


def user_on(package: TariffId, *, bought: datetime = NOW) -> User:
    days = tariff_of(package).package_days
    assert days is not None
    return User(
        id=1,  # type: ignore[arg-type]
        messenger=MessengerKind.TELEGRAM,
        external_id="1",
        tariff=package,
        referral_code="code1",
        support_number=support.generate_number(),
        created_at=bought - timedelta(days=100),
        tariff_expires_at=bought + timedelta(days=days),
        norm_since=bought,
        bonus_images=2,
        bonus_presentations=1,
    )


def period(user: User, now: datetime) -> tuple[datetime, datetime]:
    found = norm_period(user, now, free_days=30, paid_days=30)
    return found.start, found.end


def test_three_packages_with_the_numbers_from_the_table() -> None:
    assert PACKAGES == (TariffId.WEEK, TariffId.MONTH, TariffId.HALFYEAR)
    for package, numbers in TABLE.items():
        tariff = TARIFFS[package]
        assert tariff.is_package
        assert (
            tariff.package_stars,
            tariff.package_days,
            tariff.daily_messages,
            tariff.monthly_images,
            tariff.monthly_presentations,
            tariff.monthly_documents,
        ) == numbers


def test_packages_are_not_subscriptions() -> None:
    """Подписочные тарифы — сами по себе; пакет не попадает в их витрину."""
    assert not set(PACKAGES) & set(PAID_TARIFFS)
    for tariff_id in PAID_TARIFFS:
        assert not TARIFFS[tariff_id].is_package
    assert not TARIFFS[TariffId.FREE].is_package


@pytest.mark.parametrize("package", list(TABLE), ids=lambda each: each.value)
def test_the_norm_lasts_the_whole_term(package: TariffId) -> None:
    """Полгода — один период на 180 дней, а не шесть месячных."""
    user = user_on(package)
    days = TABLE[package][1]
    end = NOW + timedelta(days=days)

    assert period(user, NOW) == (NOW, end)
    assert period(user, NOW + timedelta(days=days) - timedelta(minutes=1)) == (
        NOW,
        end,
    )


def test_what_is_spent_stays_spent_for_the_whole_term() -> None:
    """Потратил 100 картинок полугодового пакета за два месяца — осталось 20
    и бонус, а не новая сотня с новым месяцем."""
    user = user_on(TariffId.HALFYEAR)
    later = NOW + timedelta(days=61)
    tariff = tariff_of(active_tariff(user.tariff, user.tariff_expires_at, later))

    found = allowance(
        user,
        Usage(day=NOW.date()),
        tariff,
        LimitKind.IMAGES,
        period_usage=PeriodUsage(images_used=100),
    )

    assert period(user, later)[0] == NOW
    assert found.monthly_left == 20
    assert found.total_left == 22, "бонус за друзей — как раньше, сверх пакета"


def test_messages_are_a_daily_norm() -> None:
    user = user_on(TariffId.WEEK)
    tariff = tariff_of(active_tariff(user.tariff, user.tariff_expires_at, NOW))

    found = allowance(
        user, Usage(day=NOW.date(), messages_used=40), tariff, LimitKind.MESSAGES
    )

    assert found.daily_limit == 100 and found.daily_left == 60


def test_after_the_end_the_free_norms_apply() -> None:
    user = user_on(TariffId.WEEK)
    after = NOW + timedelta(days=7)

    assert active_tariff(user.tariff, user.tariff_expires_at, after) is TariffId.FREE
    start, end = period(user, after)
    assert end - start == timedelta(days=30), "период — бесплатный, от регистрации"
    assert end > after


def test_a_package_bought_before_norms_existed_counts_from_its_end() -> None:
    """Без отметки начала нормы период — весь срок, отсчитанный от конца."""
    user = replace(user_on(TariffId.MONTH), norm_since=None)

    assert period(user, NOW + timedelta(days=3)) == (
        NOW,
        NOW + timedelta(days=30),
    )
