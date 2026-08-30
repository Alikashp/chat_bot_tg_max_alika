"""Сквозная проверка backpressure — критерий приёмки №12.

Быстрый аналог scripts/loadtest.py: тот гоняет 30 rps в течение минуты по
живому сервису, а этот за доли секунды доказывает то же самое поведение через
настоящий HTTP-стек. Отдельная проверка нужна потому, что переполнение
очереди — это состояние, до которого в обычных тестах не доходишь, а ведёт
себя сервис в нём принципиально иначе.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.adapters.telegram.intake import dedup_key
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import TELEGRAM_SECRET_HEADER, Webhook, create_app
from app.main import build_intake

SECRET = "backpressure-test-secret"
PATH = "/webhook/telegram"
CAPACITY = 2
WebClient = TestClient[web.Request, web.Application]


@dataclass
class Harness:
    client: WebClient
    queue: JobQueue[dict[str, Any]]
    release: asyncio.Event
    handled: list[int]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    release = asyncio.Event()
    handled: list[int] = []

    async def handler(update: dict[str, Any]) -> None:
        await release.wait()
        handled.append(update["update_id"])

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "test", handler, capacity=CAPACITY, workers=1
    )
    queue.start()

    app = create_app(
        webhooks=[
            Webhook(
                messenger="telegram",
                path=PATH,
                secret_header=TELEGRAM_SECRET_HEADER,
                secret=SECRET,
                submit=build_intake(
                    queue,
                    Deduplicator(ttl_seconds=60, max_keys=1000),
                    messenger="telegram",
                    key_of=dedup_key,
                ),
            ),
        ],
        health=lambda: {
            "status": "ok",
            "queues": [{"pending": queue.stats().pending}],
        },
    )
    client: WebClient = TestClient(TestServer(app))
    await client.start_server()

    yield Harness(client=client, queue=queue, release=release, handled=handled)

    release.set()
    await queue.drain(timeout=2.0)
    await client.close()


async def _post(harness: Harness, update_id: int) -> int:
    response = await harness.client.post(
        PATH,
        json={"update_id": update_id},
        headers={TELEGRAM_SECRET_HEADER: SECRET},
    )
    return response.status


async def test_overload_answers_503_and_never_times_out(harness: Harness) -> None:
    """Под перегрузкой сервис отвечает быстро и честно, а не зависает."""
    harness.release.clear()

    statuses = await asyncio.gather(
        *(asyncio.wait_for(_post(harness, index), timeout=2.0) for index in range(50))
    )

    assert set(statuses) <= {200, 503}
    assert statuses.count(503) > 0, "backpressure не сработал"
    # Один воркер держит задачу, ещё CAPACITY ждут в очереди.
    assert statuses.count(200) <= CAPACITY + 1


async def test_queue_never_exceeds_capacity(harness: Harness) -> None:
    """Очередь не растёт бесконечно — в этом весь смысл ограничения."""
    harness.release.clear()

    for index in range(100):
        await _post(harness, index)

    assert harness.queue.stats().pending <= CAPACITY


async def test_service_recovers_after_the_burst(harness: Harness) -> None:
    """Отказ — состояние временное: после разбора очереди приём возобновляется."""
    harness.release.clear()
    for index in range(50):
        await _post(harness, index)

    harness.release.set()
    assert await harness.queue.join(timeout=2.0)

    assert await _post(harness, 1000) == 200


async def test_rejected_updates_are_not_processed(harness: Harness) -> None:
    """Отвергнутое не должно всплыть позже: мессенджер пришлёт его заново."""
    harness.release.clear()
    for index in range(50):
        await _post(harness, index)

    harness.release.set()
    assert await harness.queue.join(timeout=2.0)

    assert len(harness.handled) <= CAPACITY + 1
