"""Экран тарифов (§2.8).

Три карточки, без сравнительной таблицы: таблица заставляет сравнивать, а
карточка — выбрать.

Оплата появится на фазе 8. До неё кнопка ведёт на честную заглушку, а не в
никуда: с экрана есть выход, и там же лежит бесплатный способ поднять лимиты.
"""

from __future__ import annotations

from app.core import texts
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session
from app.core.tariffs import PAID_TARIFFS


async def show(deps: Deps, session: Session) -> None:
    """Показывает три карточки, каждую отдельным сообщением.

    Отдельными сообщениями, а не одним: в одно уместились бы шесть строк, а
    больше пяти в сообщении не бывает (§2.9). Плюс у каждой карточки своя
    кнопка выбора.
    """
    for tariff_id in PAID_TARIFFS:
        screen = texts.tariff_card(tariff_id)
        await deps.messenger.send_text(
            session.chat,
            screen.text,
            keyboard=keyboards.tariff_card(tariff_id),
            show_menu=False,
        )


async def payments_not_ready(deps: Deps, session: Session) -> None:
    """Заглушка оплаты до фазы 8."""
    screen = texts.payments_soon()
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.payments_soon()
    )
