"""Пейволлы внутри пакета (сессия 17, шаг 1, К1).

У пакета нет «месяца»: картинки, доклады и презентации даются на весь срок.
Пейволл называет срок по факту — до какой даты действует пакет — и что
можно взять новый пакет, остатки сложатся. Название пакета «Месяц» — имя, а
не срок, и в счёт не идёт.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.core import texts
from app.core.actions import Action
from app.core.limits import LimitKind, current_day
from app.core.models import TariffId, User
from app.core.scenarios import paywall
from app.core.scenarios.deps import Deps, session_for
from tests.fakes import FakeMessenger

KINDS = [
    LimitKind.PRESENTATIONS,
    LimitKind.IMAGES,
    LimitKind.DOCUMENTS,
    LimitKind.MESSAGES,
]


@pytest.fixture
def telegram(deps: Deps) -> Deps:
    return replace(
        deps,
        cards=None,
        settings=replace(
            deps.settings, card_sales_enabled=False, packages_enabled=True
        ),
    )


async def on_package(deps: Deps, user: User, package: TariffId, days: int) -> str:
    """Человек на пакете до ``now + days``; возвращает дату конца словами."""
    ends = deps.now() + timedelta(days=days)
    await deps.storage.set_tariff(user.id, package, ends)
    return texts.format_date(current_day(ends, deps.settings.timezone))


def without_the_name(text: str) -> str:
    return text.replace("«Месяц»", "«…»")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("package", "days"),
    [(TariffId.WEEK, 7), (TariffId.MONTH, 30), (TariffId.HALFYEAR, 150)],
    ids=["week", "month", "halfyear"],
)
async def test_a_package_paywall_names_its_end_and_never_a_month(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    package: TariffId,
    days: int,
    kind: LimitKind,
) -> None:
    until = await on_package(telegram, user, package, days)
    found = await telegram.storage.get_user_by_id(user.id)
    assert found is not None

    await paywall.show(telegram, session_for(telegram, found), kind)

    sent = messenger.last_text
    assert f"«{texts.TARIFF_TITLES[package]}» действует до {until}" in sent.text
    assert "остатки сложатся" in sent.text
    assert "месяц" not in without_the_name(sent.text).lower(), sent.text
    assert sent.keyboard is not None
    assert sent.keyboard.rows[0][0].action == Action.OPEN_TARIFFS


@pytest.mark.parametrize(
    ("kind", "first_line"),
    [
        (LimitKind.PRESENTATIONS, "Презентации пакета закончились 😔"),
        (LimitKind.IMAGES, "Картинки пакета закончились 😔"),
        (LimitKind.DOCUMENTS, "Доклады пакета закончились 😔"),
        (LimitKind.MESSAGES, "Сообщения на сегодня закончились 😔"),
    ],
)
async def test_the_first_line_says_what_ran_out(
    telegram: Deps,
    user: User,
    messenger: FakeMessenger,
    kind: LimitKind,
    first_line: str,
) -> None:
    await on_package(telegram, user, TariffId.WEEK, 7)
    found = await telegram.storage.get_user_by_id(user.id)
    assert found is not None

    await paywall.show(telegram, session_for(telegram, found), kind)

    assert messenger.last_text.text.split("\n")[0] == first_line


@pytest.mark.parametrize("kind", [LimitKind.IMAGES, LimitKind.PRESENTATIONS])
async def test_the_friend_button_stays(
    telegram: Deps, user: User, messenger: FakeMessenger, kind: LimitKind
) -> None:
    """Бонус за друга — как раньше: второй выход с пейволла на месте."""
    await on_package(telegram, user, TariffId.WEEK, 7)
    found = await telegram.storage.get_user_by_id(user.id)
    assert found is not None

    await paywall.show(telegram, session_for(telegram, found), kind)

    keyboard = messenger.last_text.keyboard
    assert keyboard is not None
    actions = [button.action for row in keyboard.rows for button in row]
    assert Action.INVITE_FRIEND in actions


async def test_a_subscription_keeps_its_monthly_paywall(
    deps: Deps, user: User, messenger: FakeMessenger
) -> None:
    """Подписки (MAX, прежние Лайт/Про/Макс) — с прежними словами."""
    await deps.storage.set_tariff(
        user.id, TariffId.LITE, deps.now() + timedelta(days=40)
    )
    found = await deps.storage.get_user_by_id(user.id)
    assert found is not None

    await paywall.show(deps, session_for(deps, found), LimitKind.IMAGES)

    assert "на этот месяц закончились" in messenger.last_text.text
