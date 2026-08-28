"""Тесты очереди задач: ёмкость, backpressure, устойчивость, завершение."""

from __future__ import annotations

import asyncio

import pytest

from app.infra.queue import JobQueue


async def test_submitted_job_is_executed() -> None:
    done = asyncio.Event()

    async def handler(value: int) -> None:
        assert value == 1
        done.set()

    queue: JobQueue[int] = JobQueue("test", handler, capacity=10, workers=2)
    queue.start()

    assert queue.submit(1) is True
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await queue.drain(timeout=1.0)


async def test_all_jobs_are_executed_exactly_once() -> None:
    seen: list[int] = []

    async def handler(value: int) -> None:
        seen.append(value)

    queue: JobQueue[int] = JobQueue("test", handler, capacity=100, workers=4)
    queue.start()

    for value in range(50):
        assert queue.submit(value) is True
    await queue.drain(timeout=2.0)

    assert sorted(seen) == list(range(50))


async def test_overflow_is_refused_not_queued() -> None:
    """Главный тест фазы: за пределами ёмкости приходит честный отказ.

    Без этого очередь растёт неограниченно, и вместо отказа мы получаем
    медленную деградацию до исчерпания памяти.
    """
    release = asyncio.Event()

    async def handler(_: int) -> None:
        await release.wait()

    queue: JobQueue[int] = JobQueue("test", handler, capacity=3, workers=1)
    queue.start()

    # Один воркер заберёт первую задачу, ещё три поместятся в очередь.
    accepted = [queue.submit(value) for value in range(10)]

    assert accepted.count(True) <= 4
    assert accepted.count(False) >= 6
    assert queue.stats().rejected >= 6

    release.set()
    await queue.drain(timeout=2.0)


async def test_capacity_frees_up_after_jobs_complete() -> None:
    """Отказ — состояние временное, а не постоянное."""
    release = asyncio.Event()

    async def handler(_: int) -> None:
        await release.wait()

    queue: JobQueue[int] = JobQueue("test", handler, capacity=1, workers=1)
    queue.start()

    while queue.submit(0):
        pass
    assert queue.submit(0) is False

    release.set()
    await asyncio.sleep(0.05)

    assert queue.submit(0) is True
    await queue.drain(timeout=2.0)


async def test_failing_job_does_not_kill_the_worker() -> None:
    """Одна кривая задача не должна выбивать воркер из пула."""
    survived: list[int] = []

    async def handler(value: int) -> None:
        if value == 0:
            raise RuntimeError("провал")
        survived.append(value)

    queue: JobQueue[int] = JobQueue("test", handler, capacity=10, workers=1)
    queue.start()

    for value in range(5):
        queue.submit(value)
    await queue.drain(timeout=2.0)

    assert survived == [1, 2, 3, 4]
    assert queue.stats().failed == 1


async def test_nothing_is_accepted_after_stop() -> None:
    async def handler(_: int) -> None:
        return None

    queue: JobQueue[int] = JobQueue("test", handler, capacity=10, workers=1)
    queue.start()
    queue.stop_accepting()

    assert queue.submit(1) is False


async def test_drain_finishes_accepted_jobs() -> None:
    """Graceful shutdown: принятое доделываем, новое не берём."""
    finished: list[int] = []

    async def handler(value: int) -> None:
        await asyncio.sleep(0.01)
        finished.append(value)

    queue: JobQueue[int] = JobQueue("test", handler, capacity=20, workers=2)
    queue.start()
    for value in range(10):
        queue.submit(value)

    abandoned = await queue.drain(timeout=2.0)

    assert abandoned == 0
    assert len(finished) == 10


async def test_drain_gives_up_after_timeout() -> None:
    async def handler(_: int) -> None:
        await asyncio.sleep(3600)

    queue: JobQueue[int] = JobQueue("test", handler, capacity=10, workers=1)
    queue.start()
    for value in range(4):
        queue.submit(value)

    abandoned = await queue.drain(timeout=0.05)

    assert abandoned == 4


async def test_stats_report_the_queue_state() -> None:
    release = asyncio.Event()

    async def handler(_: int) -> None:
        await release.wait()

    queue: JobQueue[int] = JobQueue("test", handler, capacity=5, workers=1)
    queue.start()
    queue.submit(1)
    queue.submit(2)
    await asyncio.sleep(0)

    stats = queue.stats()
    assert stats.name == "test"
    assert stats.capacity == 5
    assert stats.workers == 1
    assert stats.in_flight + stats.pending == 2
    assert stats.accepted == 2

    release.set()
    await queue.drain(timeout=2.0)


@pytest.mark.parametrize(("capacity", "workers"), [(0, 1), (1, 0), (-1, 1)])
async def test_invalid_configuration_is_rejected(capacity: int, workers: int) -> None:
    async def handler(_: int) -> None:
        return None

    with pytest.raises(ValueError):
        JobQueue("test", handler, capacity=capacity, workers=workers)
