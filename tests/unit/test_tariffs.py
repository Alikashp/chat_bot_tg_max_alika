"""Тесты тарифов: числа из задания и цена в звёздах."""

from __future__ import annotations

import pytest

from app.core.models import TariffId
from app.core.tariffs import (
    HIGHLIGHTED_TARIFF,
    PAID_TARIFFS,
    TARIFFS,
    ModelTier,
    stars_price,
    tariff_of,
)
from app.ports.ai import ImageQuality


@pytest.mark.parametrize(
    ("tariff_id", "price"),
    [
        (TariffId.FREE, 0),
        (TariffId.LITE, 299),
        (TariffId.PRO, 599),
        (TariffId.MAX, 1490),
    ],
)
def test_prices_match_the_brief(tariff_id: TariffId, price: int) -> None:
    assert tariff_of(tariff_id).price_rub == price


@pytest.mark.parametrize(
    ("tariff_id", "tier"),
    [
        (TariffId.FREE, ModelTier.ECONOMY),
        (TariffId.LITE, ModelTier.ECONOMY),
        (TariffId.PRO, ModelTier.STANDARD),
        (TariffId.MAX, ModelTier.STANDARD),
    ],
)
def test_model_tier_matches_the_brief(tariff_id: TariffId, tier: ModelTier) -> None:
    """§2.2: Free и Лайт — эконом, Про и Макс — стандарт."""
    assert tariff_of(tariff_id).model_tier is tier


def test_free_draws_in_low_quality() -> None:
    """Решение про деньги: разница в цене между low и medium — почти порядок."""
    assert tariff_of(TariffId.FREE).image_quality is ImageQuality.LOW


@pytest.mark.parametrize("tariff_id", [TariffId.LITE, TariffId.PRO, TariffId.MAX])
def test_paid_tariffs_draw_in_medium_quality(tariff_id: TariffId) -> None:
    assert tariff_of(tariff_id).image_quality is ImageQuality.MEDIUM


def test_only_paid_tariffs_are_on_the_sales_screen() -> None:
    """На бесплатном пользователь уже сидит, продавать ему его же незачем."""
    assert PAID_TARIFFS == (TariffId.LITE, TariffId.PRO, TariffId.MAX)


def test_pro_is_the_highlighted_one() -> None:
    assert HIGHLIGHTED_TARIFF is TariffId.PRO


def test_only_free_is_free() -> None:
    assert tariff_of(TariffId.FREE).is_free is True
    assert all(not tariff_of(t).is_free for t in PAID_TARIFFS)


@pytest.mark.parametrize(
    ("tariff_id", "expected"),
    [(TariffId.LITE, 419), (TariffId.PRO, 839), (TariffId.MAX, 2086)],
)
def test_stars_price_is_forty_percent_higher(
    tariff_id: TariffId, expected: int
) -> None:
    """§2.8: в звёздах цены на 40% выше — это комиссия."""
    assert stars_price(tariff_of(tariff_id), markup=1.4) == expected


def test_stars_price_rounds_up() -> None:
    """Округление вниз означало бы платить комиссию из своего кармана."""
    assert stars_price(tariff_of(TariffId.LITE), markup=1.001) == 300


def test_markup_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="наценка"):
        stars_price(tariff_of(TariffId.LITE), markup=0.9)


def test_registry_is_immutable() -> None:
    """Тарифы — это правила, а не изменяемое состояние."""
    with pytest.raises(TypeError):
        TARIFFS[TariffId.FREE] = tariff_of(TariffId.MAX)  # type: ignore[index]
