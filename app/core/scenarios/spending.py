"""Списание лимита — главный инвариант проекта.

    Лимит не списывается за упавший запрос. Списание происходит только по
    факту успешно доставленного пользователю результата.

Не после вызова провайдера и даже не после получения ответа от него, а именно
после доставки. Если провайдер ответил, а отправить пользователю не удалось,
человек результата не увидел — значит и платить за него не должен.

Отсюда форма всех сценариев: сначала проверяем остаток, потом делаем работу,
потом доставляем, и только в самом конце списываем. Проверяется тестами в
tests/unit/test_spending.py и в каждом сценарии отдельно.
"""

from __future__ import annotations

from datetime import date

from app.core.limits import (
    Allowance,
    LimitKind,
    NormPeriod,
    Source,
    allowance,
    current_day,
    monthly_norm,
    norm_period,
    norm_renewal,
    tariff_continues,
)
from app.core.models import User
from app.core.scenarios.deps import Deps, Session
from app.ports.payments import SubscriptionStatus


async def renewing(deps: Deps, session: Session) -> bool:
    """Ждём ли очередного списания по подписке."""
    subscription = await deps.storage.get_subscription(session.user.id)
    return (
        subscription is not None
        and subscription.status != SubscriptionStatus.CANCELLED.value
    )


async def renewal(
    deps: Deps, session: Session, kind: LimitKind
) -> tuple[date, bool] | None:
    """Какого числа придёт новая норма и зависит ли это от продления.

    None — сама не придёт: следующим периодом правит тариф без такой нормы.
    Дата — день по часовому поясу человека: «новые будут 27 сентября» должно
    совпадать с его календарём, а не с UTC.
    """
    found = norm_renewal(
        session.user,
        period_of(deps, session),
        session.tariff,
        kind,
        renewing=await renewing(deps, session),
    )
    if found is None:
        return None
    return current_day(found.at, deps.settings.timezone), found.by_charge


async def period_end(deps: Deps, session: Session) -> tuple[date, bool]:
    """Конец текущего периода и продолжится ли после него нынешний тариф."""
    period = period_of(deps, session)
    continues = tariff_continues(
        session.user,
        period,
        session.tariff,
        renewing=await renewing(deps, session),
    )
    return current_day(period.end, deps.settings.timezone), continues


def period_of(deps: Deps, session: Session) -> NormPeriod:
    """Период месячной нормы, в котором идёт это обращение.

    Считается по тому же снимку человека, что и ``session.tariff``: тариф и
    его период обязаны быть об одном и том же моменте.
    """
    return norm_period(
        session.user,
        session.now,
        free_days=deps.settings.free_period_days,
        paid_days=deps.settings.subscription_days,
    )


async def current_allowance(deps: Deps, session: Session, kind: LimitKind) -> Allowance:
    """Сколько осталось у пользователя прямо сейчас.

    Пользователь перечитывается из хранилища, а не берётся из сессии. Снимок
    в сессии сделан в начале обработки, а бонусный баланс мог измениться после
    него: друг мог зайти по ссылке ровно в этот момент. Со снимком человек
    увидел бы в профиле старые цифры, а в чате — пейволл при живом подарке.
    """
    user = await deps.storage.get_user_by_id(session.user.id) or session.user
    return await _allowance(deps, session, user, kind)


async def _allowance(
    deps: Deps, session: Session, user: User, kind: LimitKind
) -> Allowance:
    usage = await deps.storage.get_usage(user.id, session.day)
    period = await deps.storage.get_period_usage(
        user.id, period_of(deps, session).start
    )
    return allowance(user, usage, session.tariff, kind, period_usage=period)


async def charge(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Списывает одну единицу. Вызывается только после доставки результата.

    Остаток перечитывается из хранилища, а не берётся из сессии: между
    проверкой и списанием прошёл вызов к провайдеру, за это время у
    пользователя могли измениться и расход, и бонусный баланс.

    Месячная норма и бонус списываются условно — только если ещё есть что
    списывать (Т3). Не вышло из нормы, потому что её параллельно доели, —
    пробуем бонус; не вышло и из него — результат отдан сверх нормы, и знать
    об этом надо, но счётчик за норму не уходит.
    """
    user = await deps.storage.get_user_by_id(session.user.id)
    if user is None:
        # Пользователь исчез между проверкой и списанием. Такого быть не
        # должно, но списывать с несуществующего нечего.
        deps.logger.error("charge_user_missing", user_id=int(session.user.id))
        return

    source = (await _allowance(deps, session, user, kind)).next_source

    if source is Source.DAILY:
        await deps.storage.add_usage(user.id, session.day, messages=1)
        return

    if source is Source.MONTHLY and await deps.storage.spend_norm(
        user.id,
        period_of(deps, session).start,
        kind,
        limit=monthly_norm(session.tariff, kind),
    ):
        return

    if source is not None and await deps.storage.spend_bonus(user.id, **_one_of(kind)):
        return

    if kind is LimitKind.MESSAGES and source is not None:
        # Бонус сообщений успели потратить параллельно. Результат человек уже
        # получил, отбирать его поздно — записываем в дневной расход: у
        # сообщений он и так переполняется только на одну единицу.
        await deps.storage.add_usage(user.id, session.day, messages=1)
        return

    # Списывать неоткуда: результат отдан сверх лимита. Одновременные задачи
    # одного пользователя ограничены (§3.4.8), так что в норме сюда не
    # попадаем, но знать о таком надо.
    deps.logger.warning("charged_over_limit", user_id=int(user.id), kind=kind.value)


def _one_of(kind: LimitKind) -> dict[str, int]:
    """Единица бонуса нужного вида — в терминах хранилища.

    Одним местом, а не четвёркой условий на каждый вызов: забытая ветка
    означала бы, что человек получил работу бесплатно, а мы этого даже не
    заметили.
    """
    return {
        "messages": 1 if kind is LimitKind.MESSAGES else 0,
        "images": 1 if kind is LimitKind.IMAGES else 0,
        "documents": 1 if kind is LimitKind.DOCUMENTS else 0,
        "presentations": 1 if kind is LimitKind.PRESENTATIONS else 0,
    }
