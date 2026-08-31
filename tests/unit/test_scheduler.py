"""Повторяющаяся фоновая задача.

Три свойства, ради которых у этого класса вообще есть код: сбой не выключает
расписание, первый проход не случается в момент старта, а остановка не рвёт
работу на середине. Все три невидимы в бою до того дня, когда становится
поздно.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infra.scheduler import Periodic


async def test_the_loop_repeats() -> None:
    ticks = 0

    async def work() -> None:
        nonlocal ticks
        ticks += 1

    task = Periodic("test", work, interval_seconds=0.01)
    task.start()
    await asyncio.sleep(0.05)
    await task.stop()

    assert ticks >= 2


async def test_a_failure_does_not_stop_the_schedule() -> None:
    """Иначе одна ошибка в одном списании тихо выключила бы списания вообще."""
    ticks = 0

    async def work() -> None:
        nonlocal ticks
        ticks += 1
        raise RuntimeError("проход не удался")

    task = Periodic("test", work, interval_seconds=0.01)
    task.start()
    await asyncio.sleep(0.05)
    await task.stop()

    assert ticks >= 2


async def test_the_first_pass_waits_for_the_interval() -> None:
    """При выкатке несколько экземпляров стартуют почти одновременно.

    Мгновенный проход у каждого означал бы дружный залп запросов к провайдеру
    ровно в тот момент, когда сервис ещё разогревается.
    """
    ticks = 0

    async def work() -> None:
        nonlocal ticks
        ticks += 1

    task = Periodic("test", work, interval_seconds=10)
    task.start()
    await asyncio.sleep(0.02)
    await task.stop()

    assert ticks == 0


async def test_stopping_waits_for_the_pass_in_flight() -> None:
    """Прервать проход между «деньги списали» и «тариф выдали» нельзя."""
    finished = False

    async def work() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    task = Periodic("test", work, interval_seconds=0.01)
    task.start()
    await asyncio.sleep(0.02)
    await task.stop()

    assert finished is True


async def test_an_interval_must_be_positive() -> None:
    """Нулевой интервал — это не «почаще», а занятый навсегда процессор."""
    with pytest.raises(ValueError, match="интервал"):
        Periodic("test", _nothing, interval_seconds=0)


async def test_stopping_a_task_that_never_started_is_harmless() -> None:
    await Periodic("test", _nothing, interval_seconds=1).stop()


async def _nothing() -> None:
    return None
