"""Покупка пакета поверх того, что у человека уже есть (сессия 16, П3).

Правило заказчика: остатки картинок, презентаций и докладов складываются с
новым пакетом, срок — более поздняя из дат, дневная норма сообщений —
большая из двух. Ничего не теряется: оплаченное раньше не сгорает от того,
что человек купил ещё.

Модуль чистый: остатки ему передаёт сценарий, который прочитал расход.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.limits import LimitKind, monthly_norm
from app.core.models import PackNorms, TariffId
from app.core.tariffs import Tariff


@dataclass(frozen=True, slots=True)
class PackGrant:
    """Что запишет выдача пакета: какой тариф, до какого момента и с какими
    нормами."""

    tariff: TariffId
    expires_at: datetime
    norms: PackNorms


def stack(
    bought: Tariff,
    *,
    now: datetime,
    current: Tariff,
    current_expires_at: datetime | None,
    left: Mapping[LimitKind, int],
) -> PackGrant:
    """Нормы и срок после покупки пакета ``bought``.

    ``current`` — тариф, который действует сейчас (бесплатный, если ничего не
    действует), с его настоящими нормами; ``left`` — сколько от них осталось
    в текущем периоде. Бесплатные нормы — не оплаченное, и к пакету они не
    прибавляются; бонусы за друзей и подарки живут отдельно и не трогаются.

    Называется тот пакет, что кончается позже: по нему человек узнаёт срок в
    профиле, и его же модель отвечает до конца. При равных сроках — новый.
    """
    assert bought.package_days is not None, "складываются только пакеты"
    ends = now + timedelta(days=bought.package_days)
    # Истёкший тариф сюда приходит бесплатным: складывать нечего.
    if current.is_free or current_expires_at is None:
        return PackGrant(
            tariff=bought.id,
            expires_at=ends,
            norms=PackNorms(
                messages=bought.daily_messages,
                images=bought.monthly_images,
                documents=bought.monthly_documents,
                presentations=bought.monthly_presentations,
            ),
        )

    keeps_current = current_expires_at > ends

    def total(kind: LimitKind) -> int:
        return max(0, left.get(kind, 0)) + monthly_norm(bought, kind)

    return PackGrant(
        tariff=current.id if keeps_current else bought.id,
        expires_at=current_expires_at if keeps_current else ends,
        norms=PackNorms(
            messages=max(current.daily_messages, bought.daily_messages),
            images=total(LimitKind.IMAGES),
            documents=total(LimitKind.DOCUMENTS),
            presentations=total(LimitKind.PRESENTATIONS),
        ),
    )
