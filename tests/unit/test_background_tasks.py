"""Тесты фоновых задач и graceful shutdown (§3.4.7)."""

from __future__ import annotations

import asyncio

from app.infra.tasks import BackgroundTasks


async def test_spawned_task_runs() -> None:
    tasks = BackgroundTasks()
    done = asyncio.Event()

    async def work() -> None:
        done.set()

    assert tasks.spawn(work()) is True
    await tasks.drain(timeout=1.0)
    assert done.is_set()


async def test_drain_waits_for_running_task() -> None:
    """Воркерам дают домолотить текущее — это и есть graceful shutdown."""
    tasks = BackgroundTasks()
    finished = False

    async def work() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    tasks.spawn(work())
    abandoned = await tasks.drain(timeout=1.0)

    assert finished is True
    assert abandoned == 0


async def test_drain_gives_up_after_timeout() -> None:
    """Ждать бесконечно нельзя: Railway пришлёт SIGKILL."""
    tasks = BackgroundTasks()

    async def forever() -> None:
        await asyncio.sleep(3600)

    tasks.spawn(forever())
    abandoned = await tasks.drain(timeout=0.05)

    assert abandoned == 1
    assert tasks.running == 0


async def test_no_new_tasks_after_stop_accepting() -> None:
    tasks = BackgroundTasks()
    started = False

    async def work() -> None:
        nonlocal started
        started = True

    tasks.stop_accepting()

    assert tasks.spawn(work()) is False
    await asyncio.sleep(0)
    assert started is False


async def test_failing_task_does_not_break_the_set() -> None:
    """Упавшая задача логируется и не мешает остальным."""
    tasks = BackgroundTasks()
    survivor = asyncio.Event()

    async def boom() -> None:
        raise RuntimeError("провал")

    async def work() -> None:
        survivor.set()

    tasks.spawn(boom())
    tasks.spawn(work())
    await tasks.drain(timeout=1.0)

    assert survivor.is_set()
    assert tasks.running == 0
