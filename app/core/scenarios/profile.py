"""Профиль (§2.6).

Четыре числа, и все настоящие: тариф, израсходованные сообщения, остаток
картинок, приглашённые друзья. Два выхода — тарифы и своя ссылка.
"""

from __future__ import annotations

from app.core import texts
from app.core.limits import LimitKind, daily_messages
from app.core.scenarios import keyboards, spending
from app.core.scenarios.deps import Deps, Session


async def show(deps: Deps, session: Session) -> None:
    """Показывает профиль с реальными цифрами."""
    usage = await deps.storage.get_usage(session.user.id, session.day)
    images = await spending.current_allowance(deps, session, LimitKind.IMAGES)
    friends = await deps.storage.count_referrals(session.user.id)
    # Кнопка подписки нужна тому, у кого подписка есть: §4.14 оферты обещает
    # отмену «в разделе Профиль», и вести туда надо отсюда. Остальным она
    # показывала бы экран о том, что смотреть нечего.
    subscription = await deps.storage.get_subscription(session.user.id)

    screen = texts.profile(
        tariff_id=session.tariff.id,
        messages_used=usage.messages_used,
        messages_limit=daily_messages(session.tariff),
        images_left=images.total_left,
        friends=friends,
        user_number=(
            session.user.support_number if deps.settings.show_user_number else None
        ),
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.profile(has_subscription=subscription is not None),
    )
