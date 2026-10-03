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
from datetime import datetime
from enum import StrEnum
from math import ceil
from types import MappingProxyType

from app.core.models import TariffId
from app.ports.ai import ImageQuality

#: Валюты, в которых мы берём деньги.
#:
#: Звёзды — не деньги в привычном смысле, но учёт им нужен тот же: код
#: валюты уезжает и в заказ, и в подписку, и в то, как показывается сумма.
#: Одно место на весь проект, потому что разойтись здесь означало бы
#: показать одну цену, а списать другую.
RUB = "RUB"
STARS = "XTR"


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
    #: Сообщения — единственное, что считается по суткам.
    daily_messages: int
    #: Картинки, доклады и презентации считаются месячной нормой: период
    #: платного тарифа начинается с оплаты, бесплатного — с регистрации
    #: (core/limits.py). Остаток на следующий период не переносится.
    monthly_images: int
    monthly_documents: int
    monthly_presentations: int
    model_tier: ModelTier
    image_quality: ImageQuality
    #: Разовая выдача при регистрации — бесплатному тарифу, где у докладов и
    #: презентаций нормы нет. Ложится в бонус и не сгорает. Здесь, а не в
    #: настройках, чтобы вся таблица норм жила в одном месте (Т1).
    one_time_documents: int = 0
    one_time_presentations: int = 0

    @property
    def is_free(self) -> bool:
        return self.id is TariffId.FREE


#: Реестр тарифов (§2.8) — единственное место, где заданы нормы (Т1).
#:
#: Про качество картинок. Все тарифы рисуют в medium, включая бесплатный, и
#: это решение заказчика, принятое с открытыми глазами: разница в цене между
#: low и medium — почти порядок (docs/research.md §4.3).
#:
#: Причина не в щедрости. На low модель отрисовывает лицо теми пикселями, за
#: которые ей заплатили, и черты уплывают — приколы с фото возвращают человеку
#: не его лицо. А приколы и есть тот крючок, ради которого он остаётся: на
#: испорченном первом впечатлении экономить дороже, чем на картинке.
#:
#: Пользователь про качество нигде не спрашивается и ничего о нём не читает.
TARIFFS: Mapping[TariffId, Tariff] = MappingProxyType(
    {
        TariffId.FREE: Tariff(
            id=TariffId.FREE,
            price_rub=0,
            daily_messages=20,
            # Три картинки в месяц — норма, а не разовая выдача: период
            # бесплатного тарифа идёт от регистрации, каждые тридцать дней.
            monthly_images=3,
            # Доклады и презентации бесплатно — только разово: это самые
            # дорогие запросы в сервисе. Дальше — подарки за друзей и тарифы.
            monthly_documents=0,
            monthly_presentations=0,
            model_tier=ModelTier.ECONOMY,
            image_quality=ImageQuality.MEDIUM,
            one_time_documents=2,
            one_time_presentations=1,
        ),
        TariffId.LITE: Tariff(
            id=TariffId.LITE,
            price_rub=299,
            daily_messages=100,
            monthly_images=20,
            monthly_documents=15,
            monthly_presentations=10,
            model_tier=ModelTier.ECONOMY,
            image_quality=ImageQuality.MEDIUM,
        ),
        TariffId.PRO: Tariff(
            id=TariffId.PRO,
            price_rub=599,
            daily_messages=100,
            monthly_images=40,
            monthly_documents=40,
            monthly_presentations=25,
            model_tier=ModelTier.STANDARD,
            image_quality=ImageQuality.MEDIUM,
        ),
        TariffId.MAX: Tariff(
            id=TariffId.MAX,
            price_rub=1490,
            daily_messages=200,
            monthly_images=150,
            monthly_documents=100,
            monthly_presentations=60,
            model_tier=ModelTier.STANDARD,
            image_quality=ImageQuality.MEDIUM,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class Trial:
    """Пробный период (фаза 11, часть 3): какой тариф, на сколько и за сколько.

    Здесь, рядом с тарифами, а не в настройках окружения: это условия
    договора, и они же стоят в тексте предложения. Окружением пробный
    период только включается (``TRIAL_ENABLED``).
    """

    tariff: TariffId
    days: int
    price_rub: int


#: 3 дня тарифа «Лайт» за 1 ₽, затем обычная цена Лайта каждые 30 дней.
TRIAL = Trial(tariff=TariffId.LITE, days=3, price_rub=1)

#: Порядок показа карточек на экране тарифов (§2.8). Бесплатного тут нет:
#: экран продаёт платные, а на бесплатном пользователь уже сидит.
PAID_TARIFFS: tuple[TariffId, ...] = (TariffId.LITE, TariffId.PRO, TariffId.MAX)

#: Какой тариф помечен как популярный (§2.8: «Про — ⭐ популярный»).
HIGHLIGHTED_TARIFF: TariffId = TariffId.PRO


def tariff_of(tariff_id: TariffId) -> Tariff:
    """Возвращает тариф по идентификатору."""
    return TARIFFS[tariff_id]


def stars_price(tariff: Tariff, *, markup: float, rub_per_star: float) -> int:
    """Цена в звёздах Telegram (§2.8).

    Два разных умножения, и путать их нельзя. Наценка — наше решение: Telegram
    берёт комиссию со звёздных платежей, и §2.8 требует поднять цену на 40%,
    чтобы её не платить из своего кармана. Курс — внешняя величина: звезда не
    равна рублю, и сколько она стоит, знает только прайс Telegram.

    Округляем вверх: дробных звёзд не бывает, а округление вниз означало бы
    отдавать разницу самим.
    """
    if markup < 1:
        raise ValueError("наценка не может быть меньше единицы")
    if rub_per_star <= 0:
        raise ValueError("курс звезды должен быть больше нуля")
    return ceil(tariff.price_rub * markup / rub_per_star)


def active_tariff(
    user_tariff: TariffId, expires_at: datetime | None, now: datetime
) -> TariffId:
    """Какой тариф действует прямо сейчас.

    Оплаченный тариф не вечен: у него есть срок, и после него человек
    возвращается на бесплатный. Без этой проверки одна оплата давала бы
    подписку навсегда — запись в базе есть, а читать её было бы некому.

    Бесплатный тариф не истекает: срока у него нет.
    """
    if user_tariff is TariffId.FREE:
        return TariffId.FREE
    if expires_at is None or expires_at <= now:
        return TariffId.FREE
    return user_tariff
