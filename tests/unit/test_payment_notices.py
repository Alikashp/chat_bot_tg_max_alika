"""Почему уведомление ЮKassa не подтверждало оплату сразу (сессия 14, шаг 3, О3).

В бою 15 из 18 оплат по 1 ₽ подтвердила сверка через 10–14 минут, хотя
уведомление об оплате приходит за секунды. Две причины — в нашем коде:

1. Уведомление «оплачено» проверялось одним вопросом к ЮKassa. Ответ «ещё
   не оплачено» или сбой этого вопроса — и заказ ждал сверку: уведомление уже
   принято с ответом 200, и ЮKassa его не повторяет.
2. Ключ дедупликации — номер заказа — оставался запомненным и после такой
   неудачи: повтор того же уведомления в ближайшие десять минут отбрасывался
   как дубликат.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from app.core import support
from app.core.models import MessengerKind, TariffId
from app.core.scenarios.deps import Deps
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import Outcome
from app.main import Settlement
from app.ports.payments import PaymentMethod
from tests.fakes import FakeCards, FakeLogger


def notice(order_id: str, event: str = "payment.succeeded") -> dict[str, object]:
    return {
        "event": event,
        "object": {"id": "yk-1", "metadata": {"order_id": order_id}},
    }


class Answers(FakeCards):
    """ЮKassa, которая отвечает на «оплачено ли» по очереди из списка."""

    def __init__(self, answers: Sequence[bool | Exception]) -> None:
        super().__init__()
        self.answers = list(answers)

    async def is_paid(self, external_id: str, *, expected_rub: int) -> bool:
        self.asked.append(external_id)
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, Exception):
            raise answer
        return answer


async def order_of(deps: Deps) -> str:
    user = await deps.storage.create_user(
        messenger=MessengerKind.TELEGRAM,
        external_id="7",
        referral_code="code7",
        support_number=support.generate_number(),
        bonus_images=0,
        bonus_documents=0,
    )
    order = await deps.storage.create_payment(
        user_id=user.id,
        tariff=TariffId.PRO,
        method=PaymentMethod.CARD.value,
        amount=599,
        currency="RUB",
        docs_version="2026-08-31",
    )
    await deps.storage.attach_external_id(order.id, "yk-1")
    return order.id


async def tariff(deps: Deps, order_id: str) -> TariffId:
    order = await deps.storage.get_payment(order_id)
    assert order is not None
    user = await deps.storage.get_user_by_id(order.user_id)
    assert user is not None
    return user.tariff


def settlement(deps: Deps, waits: list[float]) -> Settlement:
    from app.main import _build_settlement

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return _build_settlement({MessengerKind.TELEGRAM: deps}, sleep=sleep)


async def test_a_not_yet_paid_answer_is_asked_again(deps: Deps) -> None:
    """Причина 1: «ещё не оплачено» в момент уведомления — переспрашиваем."""
    cards = Answers([False, False, True])
    shop = replace(deps, cards=cards)
    order_id = await order_of(shop)
    waits: list[float] = []

    done = await settlement(shop, waits).handle(notice(order_id))

    assert done is True
    assert await tariff(shop, order_id) is TariffId.PRO
    assert len(cards.asked) == 3
    assert len(waits) == 2


async def test_a_failed_check_is_asked_again(deps: Deps) -> None:
    cards = Answers([ConnectionError("ЮKassa не ответила"), True])
    shop = replace(deps, cards=cards)
    order_id = await order_of(shop)

    done = await settlement(shop, []).handle(notice(order_id))

    assert done is True
    assert await tariff(shop, order_id) is TariffId.PRO


async def test_after_every_retry_the_notice_reports_it_is_not_done(
    deps: Deps, logger: FakeLogger
) -> None:
    cards = Answers([False] * 20)
    shop = replace(deps, cards=cards)
    order_id = await order_of(shop)
    waits: list[float] = []

    done = await settlement(shop, waits).handle(notice(order_id))

    assert done is False
    assert await tariff(shop, order_id) is TariffId.FREE
    assert sum(waits) <= 60, "уведомление не держит обработчик дольше минуты"
    assert len(cards.asked) == len(waits) + 1


async def test_a_cancel_notice_is_not_asked_again(deps: Deps) -> None:
    """Отмена — окончательный исход: переспрашивать незачем, ждать нечего."""
    cards = Answers([False] * 20)
    shop = replace(deps, cards=cards)
    order_id = await order_of(shop)
    waits: list[float] = []

    done = await settlement(shop, waits).handle(notice(order_id, "payment.canceled"))

    assert done is True
    assert waits == [] and len(cards.asked) == 1


async def test_every_notice_is_logged_with_its_event(
    deps: Deps, logger: FakeLogger
) -> None:
    """Без этой строки по логам не отличить «уведомление не пришло» от
    «пришло, но не подтвердило»."""
    shop = replace(deps, cards=Answers([True]))
    order_id = await order_of(shop)

    await settlement(shop, []).handle(notice(order_id))

    received = [e for e in logger.events if e.event == "payment_notice_received"]
    assert len(received) == 1
    assert received[0].fields["notice"] == "payment.succeeded"


async def test_an_unconfirmed_notice_does_not_block_its_repeat(deps: Deps) -> None:
    """Причина 2: неподтвердившее уведомление забывает свой ключ, и повтор
    того же уведомления проходит, а не отбрасывается как дубликат."""
    from app.main import _payment_notice_key, _settle_job, build_intake

    cards = Answers([False] * 5 + [True])
    shop = replace(deps, cards=cards)
    order_id = await order_of(shop)
    dedup = Deduplicator(ttl_seconds=600, max_keys=100)

    async def sleep(seconds: float) -> None:
        return None

    from app.main import _build_settlement

    job = _settle_job(
        _build_settlement({MessengerKind.TELEGRAM: shop}, sleep=sleep), dedup
    )
    queue: JobQueue[dict[str, Any]] = JobQueue(
        "payment-notices",
        job,
        capacity=5,
        workers=1,
    )
    submit = build_intake(
        queue,
        dedup,
        messenger="yookassa",
        key_of=_payment_notice_key,
    )
    queue.start()

    assert submit(notice(order_id)) is Outcome.ACCEPTED
    await queue.join(timeout=5)
    assert await tariff(shop, order_id) is TariffId.FREE

    assert submit(notice(order_id)) is Outcome.ACCEPTED
    await queue.join(timeout=5)
    assert await tariff(shop, order_id) is TariffId.PRO

    assert submit(notice(order_id)) is Outcome.DUPLICATE
    await queue.drain(timeout=1)


async def test_an_odd_event_name_is_not_copied_into_the_log(
    deps: Deps, logger: FakeLogger
) -> None:
    """Уведомление не подписано: что в нём написано — не наше, в лог идёт
    только название события вида «слово.слово»."""
    shop = replace(deps, cards=Answers([True]))
    order_id = await order_of(shop)

    await settlement(shop, []).handle(notice(order_id, "payment.succeeded; drop"))

    (received,) = [e for e in logger.events if e.event == "payment_notice_received"]
    assert received.fields["notice"] == "other"
