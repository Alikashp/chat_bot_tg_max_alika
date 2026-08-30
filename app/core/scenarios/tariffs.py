"""Экран тарифов (§2.8).

Одно сообщение, три блока, три кнопки. Сравнение здесь и есть суть экрана:
человек выбирает не «брать или нет», а «какой из трёх».

Оплата появится на фазе 8. До неё кнопка ведёт на честную заглушку, а не в
никуда: с экрана есть выход, и там же лежит бесплатный способ поднять лимиты.
"""

from __future__ import annotations

from app.core import texts
from app.core.scenarios import keyboards
from app.core.scenarios.deps import Deps, Session


async def show(deps: Deps, session: Session) -> None:
    """Показывает все три тарифа одним сообщением с тремя кнопками.

    Одним, а не тремя: тремя сравнить их нельзя — пока листаешь до третьего,
    первое уже ушло за экран, а выбирают именно сравнением.
    """
    screen = texts.tariffs_screen()
    await deps.messenger.send_text(
        session.chat,
        screen.text,
        keyboard=keyboards.tariffs(),
        show_menu=False,
    )


async def payments_not_ready(deps: Deps, session: Session) -> None:
    """Заглушка оплаты до фазы 8."""
    screen = texts.payments_soon()
    await deps.messenger.send_text(
        session.chat, screen.text, keyboard=keyboards.payments_soon()
    )
