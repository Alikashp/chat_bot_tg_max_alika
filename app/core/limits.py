"""Лимиты: две корзины и правила их расходования.

**Дневная квота** приходит из тарифа и каждые сутки восстанавливается целиком.
**Бонусный баланс** копится и не сгорает.

Сообщения живут в дневной квоте: двадцать в день на бесплатном тарифе и
больше на платных. Картинки на бесплатном тарифе не восстанавливаются вовсе —
их дневная норма равна нулю, а всё бесплатное приходит разово и оседает в
бонусе: три при регистрации, по две за каждого приглашённого друга и две за
подписку на канал. Дальше — тарифы, и там дневная норма снова появляется.

Отсюда и порядок списания: сначала дневная квота, потом бонус. Обратный
означал бы, что подарок за друга растворяется в первый же день у платящего, —
а он должен ощущаться как продолжение работы после того, как дневное
кончилось. На бесплатном тарифе дневного просто нет, и весь расход идёт из
бонуса.

Модуль чистый: ни одного обращения к хранилищу. Он отвечает на вопросы
«сколько осталось» и «откуда списывать», а сами списания выполняют сценарии.
Так решение о порядке списания остаётся в одном месте и не расползается по
реализациям хранилища.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.core.models import Usage, User
from app.core.tariffs import Tariff


class LimitKind(StrEnum):
    """Что именно расходуется."""

    MESSAGES = "messages"
    IMAGES = "images"


class Source(StrEnum):
    """Откуда списывать очередную единицу."""

    DAILY = "daily"
    BONUS = "bonus"


@dataclass(frozen=True, slots=True)
class Allowance:
    """Сколько пользователю осталось прямо сейчас."""

    kind: LimitKind
    daily_limit: int
    daily_used: int
    bonus: int

    @property
    def daily_left(self) -> int:
        """Остаток дневной квоты.

        Не даём уйти в минус: если лимит тарифа понизился (например, платная
        подписка кончилась), израсходованное может оказаться больше квоты.
        Это не долг, это просто ноль.
        """
        return max(0, self.daily_limit - self.daily_used)

    @property
    def total_left(self) -> int:
        """Сколько всего осталось — с учётом бонуса."""
        return self.daily_left + self.bonus

    @property
    def exhausted(self) -> bool:
        """Кончилось ли всё. Только в этом случае показывается пейволл (§2.5)."""
        return self.total_left <= 0

    @property
    def next_source(self) -> Source | None:
        """Откуда спишется следующая единица; None — если списывать неоткуда."""
        if self.daily_left > 0:
            return Source.DAILY
        if self.bonus > 0:
            return Source.BONUS
        return None


def daily_messages(tariff: Tariff) -> int:
    """Дневная норма сообщений."""
    return tariff.daily_messages


def daily_images(tariff: Tariff) -> int:
    """Дневная норма картинок.

    На бесплатном тарифе она нулевая: там картинки не восстанавливаются по
    суткам, а выдаются разово и лежат в бонусе. Отдельной функцией, а не
    обращением к полю, — ради симметрии с сообщениями и ради одного места,
    где эту норму читают.
    """
    return tariff.daily_images


def allowance(user: User, usage: Usage, tariff: Tariff, kind: LimitKind) -> Allowance:
    """Считает остаток по виду ресурса."""
    if kind is LimitKind.MESSAGES:
        return Allowance(
            kind=kind,
            daily_limit=daily_messages(tariff),
            daily_used=usage.messages_used,
            bonus=user.bonus_messages,
        )
    return Allowance(
        kind=kind,
        daily_limit=daily_images(tariff),
        daily_used=usage.images_used,
        bonus=user.bonus_images,
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
