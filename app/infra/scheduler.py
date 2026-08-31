"""Повторяющаяся фоновая работа.

Одна задача, один интервал, честная остановка. Ни cron, ни отдельного
процесса: единственное, что нужно повторять в этом сервисе, — обход подписок
раз в час, и заводить ради него внешний планировщик значило бы завести ещё
одну вещь, которая может упасть незаметно.

Три решения, ради которых этот файл существует.

**Сбой в проходе не останавливает расписание.** Исключение из работы ловится и
записывается, а следующий проход всё равно состоится. Иначе одна ошибка в
одном списании тихо выключила бы списания вообще — и заметили бы это только по
выручке.

**Ждём до, а не после.** Первый проход происходит через интервал после старта,
а не в момент запуска: при выкатке несколько экземпляров стартуют почти
одновременно, и мгновенный проход у каждого означал бы дружный залп запросов
к провайдеру.

**Остановка не рвёт работу на середине.** ``stop`` дожидается текущего прохода,
если тот уже начался: прервать его между «деньги списали» и «тариф выдали»
было бы худшим из возможных мест.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from app.infra.logging import get_logger

logger = get_logger(__name__)


class Periodic:
    """Задача, которая повторяется, пока её не остановят."""

    def __init__(
        self,
        name: str,
        work: Callable[[], Awaitable[None]],
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("интервал должен быть больше нуля")
        self._name = name
        self._work = work
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        #: Держим на время работы: по нему остановка понимает, что проход
        #: начался и его надо дождаться.
        self._running = asyncio.Lock()

    def start(self) -> None:
        """Запускает цикл. Повторный вызов ничего не делает."""
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name=self._name)
        logger.info("periodic_started", task=self._name, interval=self._interval)

    async def stop(self) -> None:
        """Останавливает цикл, дождавшись текущего прохода."""
        if self._task is None:
            return
        self._stopping.set()
        # Ждём именно замок, а не задачу: задача спит между проходами, и её
        # отмена во время сна безопасна, а во время прохода — нет.
        async with self._running:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("periodic_stopped", task=self._name)

    async def run_once(self) -> None:
        """Один проход с той же защитой от сбоя, что и в цикле."""
        async with self._running:
            try:
                await self._work()
            except Exception as error:
                # Записываем и живём дальше: следующий проход состоится.
                logger.error("periodic_failed", task=self._name, error=repr(error))

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            # Сначала ждём, потом работаем: залп из нескольких экземпляров
            # сразу после выкатки никому не нужен.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            if self._stopping.is_set():
                return
            await self.run_once()
