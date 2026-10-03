"""Повторная доставка после 503 (фаза 11, критерий И).

503 — это просьба «пришли ещё раз»: Telegram, MAX и ЮKassa так и делают.
Но ключ дедупликации запоминался раньше, чем очередь соглашалась принять
задачу, и повтор отбрасывался как уже виденный — с ответом 200. Обновление
терялось молча: человек не получал ответа, а уведомление об оплате не
доходило до выдачи тарифа.

Проверяется через настоящий HTTP-стек и для всех трёх вебхуков: ключи у них
разные, а ошибка была общей.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.adapters.max.intake import dedup_key as max_dedup_key
from app.adapters.telegram.intake import dedup_key
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import (
    MAX_SECRET_HEADER,
    TELEGRAM_SECRET_HEADER,
    Webhook,
    create_app,
)
from app.main import _payment_notice_key, build_intake

SECRET = "redelivery-test-secret"
WebClient = TestClient[web.Request, web.Application]


@dataclass(frozen=True)
class Hook:
    """Вебхук и то, как выглядят его обновления."""

    messenger: str
    path: str
    secret_header: str | None
    key_of: Callable[[dict[str, Any]], str | None]
    update: Callable[[int], dict[str, Any]]


HOOKS = [
    Hook(
        "telegram",
        "/webhook/telegram",
        TELEGRAM_SECRET_HEADER,
        dedup_key,
        lambda number: {"update_id": number},
    ),
    Hook(
        "max",
        "/webhook/max",
        MAX_SECRET_HEADER,
        max_dedup_key,
        lambda number: {
            "update_type": "message_created",
            "message": {"body": {"mid": f"mid-{number}"}},
        },
    ),
    Hook(
        "yookassa",
        "/webhook/yookassa",
        None,
        _payment_notice_key,
        lambda number: {
            "event": "payment.succeeded",
            "object": {"id": f"pay-{number}", "metadata": {"order_id": f"o-{number}"}},
        },
    ),
]


@dataclass
class Harness:
    hook: Hook
    client: WebClient
    queue: JobQueue[dict[str, Any]]
    release: asyncio.Event
    handled: list[str | None]


@pytest.fixture(params=HOOKS, ids=lambda hook: hook.messenger)
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    hook: Hook = request.param
    release = asyncio.Event()
    release.set()
    handled: list[str | None] = []

    async def handler(update: dict[str, Any]) -> None:
        await release.wait()
        handled.append(hook.key_of(update))

    # Одно место в работе и одно в ожидании: третье обновление получает 503.
    queue: JobQueue[dict[str, Any]] = JobQueue(
        hook.messenger, handler, capacity=1, workers=1
    )
    queue.start()
    app = create_app(
        webhooks=[
            Webhook(
                messenger=hook.messenger,
                path=hook.path,
                secret_header=hook.secret_header,
                secret=SECRET if hook.secret_header is not None else None,
                submit=build_intake(
                    queue,
                    Deduplicator(ttl_seconds=600, max_keys=1000),
                    messenger=hook.messenger,
                    key_of=hook.key_of,
                ),
            )
        ],
        health=lambda: {"status": "ok"},
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(hook, client, queue, release, handled)

    release.set()
    await queue.drain(timeout=2.0)
    await client.close()


async def _post(harness: Harness, number: int) -> int:
    headers = (
        {harness.hook.secret_header: SECRET}
        if harness.hook.secret_header is not None
        else {}
    )
    response = await harness.client.post(
        harness.hook.path, json=harness.hook.update(number), headers=headers
    )
    return response.status


async def test_an_update_refused_with_503_is_processed_on_redelivery(
    harness: Harness,
) -> None:
    harness.release.clear()
    assert await _post(harness, 1) == 200
    # Даём первому уйти в работу: тогда второе ждёт в очереди, а третьему
    # места нет.
    await asyncio.sleep(0)
    assert await _post(harness, 2) == 200
    assert await _post(harness, 3) == 503

    harness.release.set()
    assert await harness.queue.join(timeout=2.0)

    assert await _post(harness, 3) == 200
    assert await harness.queue.join(timeout=2.0)
    assert harness.handled.count(harness.hook.key_of(harness.hook.update(3))) == 1


async def test_an_accepted_update_is_still_not_processed_twice(
    harness: Harness,
) -> None:
    """Забывается только отвергнутое: принятое по-прежнему отсекается."""
    assert await _post(harness, 1) == 200
    assert await harness.queue.join(timeout=2.0)

    assert await _post(harness, 1) == 200
    assert await harness.queue.join(timeout=2.0)
    assert len(harness.handled) == 1
