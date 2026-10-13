"""Кнопка покупки с тарифом, который не продаётся (сессия 10, шаг 6б, С6).

Тариф приходит в данных кнопки, то есть снаружи: подделать нажатие можно,
а кнопка из старой версии может назвать тариф, которого больше нет.
Бесплатный не продаётся, неизвестный — тем более: заказа быть не должно, а
человек видит тарифы, которые купить можно.
"""

from __future__ import annotations

import pytest

from app.adapters.storage.memory import InMemoryStorage
from app.core import texts
from app.core.actions import buy_action, email_action, method_action
from app.core.models import Chat, IncomingMessage, MessengerKind, User
from app.core.router import handle
from app.core.scenarios.deps import Deps
from tests.fakes import FakeMessenger

CHAT = Chat(messenger=MessengerKind.TELEGRAM, chat_id="1")


def incoming(**fields: object) -> IncomingMessage:
    return IncomingMessage(chat=CHAT, external_user_id="1", **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("tariff", ["free", "platinum"])
@pytest.mark.parametrize(
    "press",
    [
        buy_action,
        lambda tariff: method_action("card", tariff),
        lambda tariff: method_action("stars", tariff),
    ],
    ids=["buy", "card", "stars"],
)
async def test_a_tariff_not_for_sale_opens_no_order(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    tariff: str,
    press: object,
) -> None:
    await handle(deps, incoming(action=press(tariff)))  # type: ignore[operator]

    assert storage._payments == {}
    screen = texts.tariffs_screen(with_presentations=deps.presentations_on)
    assert messenger.last_text.text == screen.text


@pytest.mark.parametrize("tariff", ["free", "platinum"])
async def test_an_email_for_a_tariff_not_for_sale_opens_no_order(
    deps: Deps,
    user: User,
    storage: InMemoryStorage,
    messenger: FakeMessenger,
    tariff: str,
) -> None:
    """«Другая почта» с подделанным тарифом: адрес не спрашиваем, заказа нет."""
    await handle(deps, incoming(action=email_action(tariff)))
    await handle(deps, incoming(text="person@example.com"))

    assert storage._payments == {}
    screen = texts.tariffs_screen(with_presentations=deps.presentations_on)
    assert screen.text in messenger.texts_said()


async def test_a_stored_email_wait_for_the_free_tariff_opens_no_order(
    deps: Deps, user: User, storage: InMemoryStorage, messenger: FakeMessenger
) -> None:
    """Ожидание почты, записанное до этой версии, тоже не ведёт к заказу."""
    from app.core import pending

    await storage.set_pending(user.id, pending.await_email("free"))

    await handle(deps, incoming(text="person@example.com"))

    assert storage._payments == {}
    screen = texts.tariffs_screen(with_presentations=deps.presentations_on)
    assert messenger.last_text.text == screen.text
