"""Тарифы: числа и правила, но не формулировки.

Здесь только то, что влияет на поведение — лимиты, класс модели, качество
картинки, цена. Всё, что пользователь читает глазами, живёт в core/texts.py.
Разделение не формальное: числа меняются по продуктовым соображениям, тексты —
по редакторским, и смешивать их в одном файле значит править одно, задевая
другое.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.core.models import TariffId
from app.ports.ai import ImageQuality


class ModelTier(StrEnum):
    """Класс модели.

    Пользователь про него не знает и выбрать не может (§2.2). Конкретные
    названия моделей приходят из конфига, а не зашиты сюда: сменить модель
    должно быть можно переменной окружения, без выкладки кода.
    """

    ECONOMY = "economy"
    STANDARD = "standard"


@dataclass(frozen=True, slots=True)
class Tariff:
    """Тариф со всеми числами, влияющими на поведение."""

    id: TariffId
    price_rub: int
    daily_messages: int
    daily_images: int
    model_tier: ModelTier
    image_quality: ImageQuality

    @property
    def is_free(self) -> bool:
        return self.id is TariffId.FREE


#: Реестр тарифов (§2.8).
#:
#: Про качество картинок. Это не фича интерфейса, а параметр стоимости:
#: разница в цене между low и medium — почти порядок (docs/research.md §4.3),
#: а бесплатная аудитория по определению не приносит выручки. Поэтому Free
#: рисует в low, платные — в medium. Пользователь про качество нигде не
#: спрашивается и ничего о нём не читает.
TARIFFS: Mapping[TariffId, Tariff] = MappingProxyType(
    {
        TariffId.FREE: Tariff(
            id=TariffId.FREE,
            price_rub=0,
            daily_messages=20,
            daily_images=3,
            model_tier=ModelTier.ECONOMY,
            image_quality=ImageQuality.LOW,
        ),
        TariffId.LITE: Tariff(
            id=TariffId.LITE,
            price_rub=299,
            daily_messages=100,
            daily_images=40,
            model_tier=ModelTier.ECONOMY,
            image_quality=ImageQuality.MEDIUM,
        ),
        TariffId.PRO: Tariff(
            id=TariffId.PRO,
            price_rub=599,
            daily_messages=100,
            daily_images=60,
            model_tier=ModelTier.STANDARD,
            image_quality=ImageQuality.MEDIUM,
        ),
        TariffId.MAX: Tariff(
            id=TariffId.MAX,
            price_rub=1490,
            daily_messages=200,
            daily_images=150,
            model_tier=ModelTier.STANDARD,
            image_quality=ImageQuality.MEDIUM,
        ),
    }
)

#: Порядок показа карточек на экране тарифов (§2.8). Бесплатного тут нет:
#: экран продаёт платные, а на бесплатном пользователь уже сидит.
PAID_TARIFFS: tuple[TariffId, ...] = (TariffId.LITE, TariffId.PRO, TariffId.MAX)

#: Какой тариф помечен как популярный (§2.8: «Про — ⭐ популярный»).
HIGHLIGHTED_TARIFF: TariffId = TariffId.PRO


def tariff_of(tariff_id: TariffId) -> Tariff:
    """Возвращает тариф по идентификатору."""
    return TARIFFS[tariff_id]


def stars_price(tariff: Tariff, markup: float) -> int:
    """Цена в звёздах Telegram — дороже на комиссию (§2.8).

    Округляем вверх до целого: платёжные системы дробных единиц не принимают,
    а округление вниз означало бы платить комиссию из своего кармана.
    """
    if markup < 1:
        raise ValueError("наценка не может быть меньше единицы")
    raw = tariff.price_rub * markup
    return int(raw) if raw == int(raw) else int(raw) + 1
