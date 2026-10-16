"""Подарки со сроком (сессия 12, шаг 4): пока это только презентация из R2.

Подарок — не бонус. Бонус бессрочный и лежит в ``users.bonus_*`` одним
числом; у подарка есть срок, и в базе должно быть видно, начислен ли он,
использован или сгорел. Поэтому каждый подарок — своя строка в ``gifts``.

Порядок трат: подарок со сроком, затем месячная норма, затем бессрочные
бонусы. Сгорающее тратится первым — иначе человек терял бы его, имея
бессрочное в запасе (``limits.Allowance.next_source``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.core.limits import LimitKind
from app.core.models import UserId

if TYPE_CHECKING:
    # Только для подсказок типов: порт хранилища сам знает про Gift.
    from app.ports.observability import Logger
    from app.ports.storage import Storage

#: Срок подарка из R2: «Действует 3 дня» — от отправки сообщения.
R2_GIFT_DAYS = 3
R2_GIFT_SOURCE = "R2"


@dataclass(frozen=True, slots=True)
class Gift:
    """Один подарок: что, откуда, до какого времени и чем кончился."""

    user_id: UserId
    kind: LimitKind
    source: str
    granted_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    expired_at: datetime | None = None


def r2_expiry(sent_at: datetime) -> datetime:
    return sent_at + timedelta(days=R2_GIFT_DAYS)


async def expire(storage: Storage, logger: Logger, now: datetime) -> None:
    """Отмечает сгоревшие подарки.

    Остаток считается по сроку, а не по этой отметке: подарок перестаёт
    тратиться ровно в свой час, даже если проход опоздал. Отметка нужна,
    чтобы в базе было видно «сгорел», а не только «не использован».
    """
    expired = await storage.expire_gifts(now)
    if expired:
        logger.info("gifts_expired", count=expired)
