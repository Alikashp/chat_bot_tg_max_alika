"""Профиль (§2.6, §4.7).

Все числа настоящие: тариф, израсходованные сообщения, остатки по тому, что
есть в меню — картинки, доклады, презентации, — и приглашённые друзья. Два
выхода — тарифы и своя ссылка.
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
    docs = await spending.current_allowance(deps, session, LimitKind.DOCUMENTS)
    decks = (
        await spending.current_allowance(deps, session, LimitKind.PRESENTATIONS)
        if deps.presentations_on
        else None
    )
    friends = await deps.storage.count_referrals(session.user.id)
    # Кнопка подписки нужна тому, у кого подписка есть: §4.14 оферты обещает
    # отмену «в разделе Профиль», и вести туда надо отсюда. Остальным она
    # показывала бы экран о том, что смотреть нечего.
    subscription = await deps.storage.get_subscription(session.user.id)
    ends, continues = await spending.period_end(deps, session)

    screen = texts.profile(
        tariff_id=session.tariff.id,
        messages_used=usage.messages_used,
        messages_limit=daily_messages(session.tariff),
        images_left=images.total_left,
        documents_left=docs.total_left,
        presentations_left=decks.total_left if decks is not None else None,
        friends=friends,
        user_number=(
            session.user.support_number if deps.settings.show_user_number else None
        ),
        period_ends=texts.format_date(ends),
        tariff_continues=continues,
    )
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.profile(has_subscription=subscription is not None),
    )
