"""За сутки до конца пакета — предложение купить снова (сессия 16, П4).

Пакет не продлевается сам: без этого сообщения человек узнал бы о конце,
только упёршись в бесплатные нормы. Одно сообщение на срок; после конца
действуют бесплатные нормы, и этим занимается ``active_tariff``, а не мы.
"""

from __future__ import annotations

from app.core import texts
from app.core.limits import current_day
from app.core.models import User
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, session_for


async def notify(deps: Deps, user: User) -> None:
    """Говорит, когда кончится пакет, и даёт купить его снова.

    Отметка — после отправки: пропустить предупреждение хуже, чем прислать
    его дважды (как у напоминания о списании).
    """
    expires_at = user.tariff_expires_at
    assert expires_at is not None
    screen = texts.package_ending(
        user.tariff,
        on=texts.format_date(current_day(expires_at, deps.settings.timezone)),
    )
    await deps.messenger.send_text(
        session_for(deps, user).chat,
        screen.text,
        keyboard=keyboards.package_again(user.tariff),
    )
    await deps.storage.mark_package_notified(user.id, expires_at)
    deps.logger.info("package_ending_notified", user_id=int(user.id))
