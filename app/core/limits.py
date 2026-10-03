"""Лимиты: три корзины и правила их расходования.

**Дневная норма** — только у сообщений: каждые сутки восстанавливается
целиком.

**Месячная норма** — у картинок, докладов и презентаций. Период платного
тарифа начинается с оплаты и длится столько, сколько оплачено; каждая оплата
и каждое продление начинают его заново. Период бесплатного — тридцать дней от
регистрации, затем следующие тридцать. Остаток не переносится: прошлый
период просто никто больше не читает.

**Бонус** копится и не сгорает: подарки за друзей и за канал, разовые выдачи
при регистрации.

Порядок списания: сначала норма (дневная или месячная — у каждого ресурса
она одна), потом бонус. Обратный означал бы, что подарок за друга
растворяется в первом же периоде у платящего, — а он должен ощущаться как
продолжение работы после того, как норма кончилась.

Модуль чистый: ни одного обращения к хранилищу. Он отвечает на вопросы
«сколько осталось», «откуда списывать» и «какой сейчас период», а сами
списания выполняют сценарии. Так решение о порядке списания остаётся в одном
месте и не расползается по реализациям хранилища.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.core.models import PeriodUsage, TariffId, Usage, User
from app.core.tariffs import Tariff, active_tariff


class LimitKind(StrEnum):
    """Что именно расходуется."""

    MESSAGES = "messages"
    IMAGES = "images"
    DOCUMENTS = "documents"
    PRESENTATIONS = "presentations"


class Source(StrEnum):
    """Откуда списывать очередную единицу."""

    DAILY = "daily"
    MONTHLY = "monthly"
    BONUS = "bonus"


@dataclass(frozen=True, slots=True)
class Allowance:
    """Сколько пользователю осталось прямо сейчас."""

    kind: LimitKind
    daily_limit: int
    daily_used: int
    bonus: int
    monthly_limit: int = 0
    monthly_used: int = 0

    @property
    def daily_left(self) -> int:
        """Остаток дневной нормы.

        Не даём уйти в минус: если лимит тарифа понизился (например, платная
        подписка кончилась), израсходованное может оказаться больше квоты.
        Это не долг, это просто ноль.
        """
        return max(0, self.daily_limit - self.daily_used)

    @property
    def monthly_left(self) -> int:
        """Остаток месячной нормы. В минус не уходит по той же причине."""
        return max(0, self.monthly_limit - self.monthly_used)

    @property
    def norm_left(self) -> int:
        """Остаток нормы тарифа — без бонуса."""
        return self.daily_left + self.monthly_left

    @property
    def total_left(self) -> int:
        """Сколько всего осталось — с учётом бонуса."""
        return self.norm_left + self.bonus

    @property
    def exhausted(self) -> bool:
        """Кончилось ли всё. Только в этом случае показывается пейволл (§2.5)."""
        return self.total_left <= 0

    @property
    def next_source(self) -> Source | None:
        """Откуда спишется следующая единица; None — если списывать неоткуда."""
        if self.daily_left > 0:
            return Source.DAILY
        if self.monthly_left > 0:
            return Source.MONTHLY
        if self.bonus > 0:
            return Source.BONUS
        return None


@dataclass(frozen=True, slots=True)
class NormPeriod:
    """Период месячной нормы: с какого момента и до какого.

    Начало служит ключом расхода в хранилище: новый период — новая запись,
    и потраченное в прошлом сюда не попадает.
    """

    start: datetime
    end: datetime


def daily_messages(tariff: Tariff) -> int:
    """Дневная норма сообщений."""
    return tariff.daily_messages


def monthly_norm(tariff: Tariff, kind: LimitKind) -> int:
    """Месячная норма тарифа по виду ресурса. У сообщений её нет."""
    if kind is LimitKind.IMAGES:
        return tariff.monthly_images
    if kind is LimitKind.DOCUMENTS:
        return tariff.monthly_documents
    if kind is LimitKind.PRESENTATIONS:
        return tariff.monthly_presentations
    return 0


def norm_period(
    user: User, now: datetime, *, free_days: int, paid_days: int
) -> NormPeriod:
    """Какой период месячной нормы идёт у человека сейчас.

    Платный тариф: отсчёт от последней выдачи оплаченного (``norm_since``)
    шагами по оплаченному сроку, но не дальше его конца. Шаги нужны тому, кто
    оплатил больше месяца вперёд: норма у него обновляется каждые тридцать
    дней, а не один раз на всё оплаченное. Подписчик, оплативший до появления
    месячных норм, отметки не имеет — его период отсчитывается от конца срока
    назад: ровно то, за что он заплатил.

    Бесплатный тариф — в том числе после конца оплаченного срока: отсчёт от
    регистрации шагами по тридцать дней. Платная норма тем самым не
    переживает свой срок: тариф кончился — и период, и норма бесплатные.
    """
    tariff = active_tariff(user.tariff, user.tariff_expires_at, now)
    if tariff is TariffId.FREE or user.tariff_expires_at is None:
        return _window(user.created_at, now, timedelta(days=free_days), until=None)
    step = timedelta(days=paid_days)
    anchor = user.norm_since or user.tariff_expires_at - step
    return _window(anchor, now, step, until=user.tariff_expires_at)


def _window(
    anchor: datetime, now: datetime, step: timedelta, *, until: datetime | None
) -> NormPeriod:
    """Отрезок длиной ``step`` от ``anchor``, внутри которого лежит ``now``."""
    start = anchor + step * ((now - anchor) // step)
    end = start + step
    if until is not None and until < end:
        end = until
    return NormPeriod(start=start, end=end)


def allowance(
    user: User,
    usage: Usage,
    tariff: Tariff,
    kind: LimitKind,
    *,
    period_usage: PeriodUsage | None = None,
) -> Allowance:
    """Считает остаток по виду ресурса.

    ``usage`` — расход за сутки (нужен сообщениям), ``period_usage`` — за
    текущий период месячной нормы (нужен всему остальному).
    """
    if kind is LimitKind.MESSAGES:
        return Allowance(
            kind=kind,
            daily_limit=daily_messages(tariff),
            daily_used=usage.messages_used,
            bonus=user.bonus_messages,
        )
    period = period_usage if period_usage is not None else PeriodUsage()
    if kind is LimitKind.PRESENTATIONS:
        used, bonus = period.presentations_used, user.bonus_presentations
    elif kind is LimitKind.DOCUMENTS:
        used, bonus = period.documents_used, user.bonus_documents
    else:
        used, bonus = period.images_used, user.bonus_images
    return Allowance(
        kind=kind,
        daily_limit=0,
        daily_used=0,
        bonus=bonus,
        monthly_limit=monthly_norm(tariff, kind),
        monthly_used=used,
    )


def current_day(now: datetime, timezone: str) -> date:
    """Какие «сегодня» сутки для пользователя.

    Сутки считаются по заданному поясу, а не по UTC: «завтра» из текста
    пейволла должно совпадать с «завтра» у человека, иначе в Москве лимиты
    обновлялись бы в три часа ночи.

    Требует момент с часовым поясом. Наивная дата привела бы к тому, что
    сутки съезжали бы на три часа незаметно для тестов.
    """
    if now.tzinfo is None:
        raise ValueError("нужен момент с часовым поясом, наивный не годится")
    return now.astimezone(ZoneInfo(timezone)).date()
