"""Тесты приёма обновления: ключ дедупликации и порядок проверок."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.adapters.telegram.intake import dedup_key
from app.infra.dedup import Deduplicator
from app.infra.queue import JobQueue
from app.infra.server import Outcome
from app.main import build_intake


def _intake(queue: JobQueue[Any], dedup: Deduplicator) -> Any:
    """Приём обновлений Telegram — с ключом его же адаптера."""
    return build_intake(queue, dedup, messenger="telegram", key_of=dedup_key)


# --- Ключ дедупликации ---------------------------------------------------


def test_key_is_built_from_update_id() -> None:
    assert dedup_key({"update_id": 42}) == "tg:42"


def test_different_updates_get_different_keys() -> None:
    assert dedup_key({"update_id": 1}) != dedup_key({"update_id": 2})


@pytest.mark.parametrize(
    "raw", [{}, {"update_id": "42"}, {"update_id": None}, {"update_id": True}]
)
def test_update_without_valid_id_has_no_key(raw: dict[str, Any]) -> None:
    """Telegram такого не присылает — значит это чужой запрос."""
    assert dedup_key(raw) is None


# --- Приём ---------------------------------------------------------------


async def _queue(
    capacity: int = 10, workers: int = 1
) -> tuple[JobQueue[dict[str, Any]], list[dict[str, Any]], asyncio.Event]:
    handled: list[dict[str, Any]] = []
    release = asyncio.Event()
    release.set()

    async def handler(update: dict[str, Any]) -> None:
        await release.wait()
        handled.append(update)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "test", handler, capacity=capacity, workers=workers
    )
    queue.start()
    return queue, handled, release


async def test_new_update_is_accepted() -> None:
    queue, handled, _ = await _queue()
    submit = _intake(queue, Deduplicator(ttl_seconds=60, max_keys=100))

    assert submit({"update_id": 1}) is Outcome.ACCEPTED

    await queue.drain(timeout=1.0)
    assert handled == [{"update_id": 1}]


async def test_repeated_update_is_not_processed_twice() -> None:
    """Двойная отрисовка картинки по одному сообщению недопустима."""
    queue, handled, _ = await _queue()
    submit = _intake(queue, Deduplicator(ttl_seconds=60, max_keys=100))

    assert submit({"update_id": 1}) is Outcome.ACCEPTED
    assert submit({"update_id": 1}) is Outcome.DUPLICATE

    await queue.drain(timeout=1.0)
    assert handled == [{"update_id": 1}]


async def test_update_without_id_is_malformed() -> None:
    queue, handled, _ = await _queue()
    submit = _intake(queue, Deduplicator(ttl_seconds=60, max_keys=100))

    assert submit({"нет": "id"}) is Outcome.MALFORMED

    await queue.drain(timeout=1.0)
    assert handled == []


async def test_overflow_is_reported() -> None:
    queue, _, release = await _queue(capacity=1, workers=1)
    release.clear()
    submit = _intake(queue, Deduplicator(ttl_seconds=60, max_keys=100))

    outcomes = [submit({"update_id": index}) for index in range(10)]

    assert Outcome.OVERLOADED in outcomes
    release.set()
    await queue.drain(timeout=1.0)


async def test_duplicate_does_not_consume_queue_capacity() -> None:
    """Иначе при ретраях мессенджера ёмкость выедается копиями одного письма."""
    queue, _, release = await _queue(capacity=2, workers=1)
    release.clear()
    dedup = Deduplicator(ttl_seconds=60, max_keys=100)
    submit = _intake(queue, dedup)

    for _ in range(20):
        submit({"update_id": 1})

    assert queue.stats().accepted == 1
    assert queue.stats().rejected == 0

    release.set()
    await queue.drain(timeout=1.0)


async def test_stopping_service_refuses_updates() -> None:
    queue, _, _ = await _queue()
    submit = _intake(queue, Deduplicator(ttl_seconds=60, max_keys=100))
    queue.stop_accepting()

    assert submit({"update_id": 1}) is Outcome.STOPPING

    await queue.drain(timeout=1.0)
