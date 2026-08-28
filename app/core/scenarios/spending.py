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

from app.core.limits import Allowance, LimitKind, Source, allowance
from app.core.scenarios.deps import Deps, Session


async def current_allowance(deps: Deps, session: Session, kind: LimitKind) -> Allowance:
    """Сколько осталось у пользователя прямо сейчас.

    Пользователь перечитывается из хранилища, а не берётся из сессии. Снимок
    в сессии сделан в начале обработки, а бонусный баланс мог измениться после
    него: друг мог зайти по ссылке ровно в этот момент. Со снимком человек
    увидел бы в профиле старые цифры, а в чате — пейволл при живом подарке.
    """
    user = await deps.storage.get_user_by_id(session.user.id) or session.user
    usage = await deps.storage.get_usage(user.id, session.day)
    return allowance(user, usage, session.tariff, kind)


async def charge(deps: Deps, session: Session, kind: LimitKind) -> None:
    """Списывает одну единицу. Вызывается только после доставки результата.

    Остаток перечитывается из хранилища, а не берётся из сессии: между
    проверкой и списанием прошёл вызов к провайдеру, за это время у
    пользователя могли измениться и расход, и бонусный баланс.
    """
    user = await deps.storage.get_user_by_id(session.user.id)
    if user is None:
        # Пользователь исчез между проверкой и списанием. Такого быть не
        # должно, но списывать с несуществующего нечего.
        deps.logger.error("charge_user_missing", user_id=int(session.user.id))
        return

    usage = await deps.storage.get_usage(user.id, session.day)
    source = allowance(user, usage, session.tariff, kind).next_source

    if source is Source.DAILY:
        await deps.storage.add_usage(
            user.id,
            session.day,
            messages=1 if kind is LimitKind.MESSAGES else 0,
            images=1 if kind is LimitKind.IMAGES else 0,
        )
        return

    if source is Source.BONUS:
        spent = await deps.storage.spend_bonus(
            user.id,
            messages=1 if kind is LimitKind.MESSAGES else 0,
            images=1 if kind is LimitKind.IMAGES else 0,
        )
        if not spent:
            # Бонус успели потратить параллельно. Результат пользователь уже
            # получил, отбирать его поздно — записываем в дневной расход.
            await deps.storage.add_usage(
                user.id,
                session.day,
                messages=1 if kind is LimitKind.MESSAGES else 0,
                images=1 if kind is LimitKind.IMAGES else 0,
            )
        return

    # Списывать неоткуда: результат отдан сверх лимита. Одновременные задачи
    # одного пользователя ограничены (§3.4.8), так что в норме сюда не
    # попадаем, но знать о таком надо.
    deps.logger.warning("charged_over_limit", user_id=int(user.id), kind=kind.value)
