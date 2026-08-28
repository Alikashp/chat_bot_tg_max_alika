"""Ограниченная очередь задач с пулом воркеров.

Заменяет неограниченный набор фоновых задач из фазы 1. Разница
принципиальная: раньше при наплыве обновлений мы создавали столько корутин,
сколько пришло запросов, и упирались в память. Теперь ёмкость задана явно, а
при её исчерпании приходит честный отказ (backpressure) вместо медленной
деградации.

Класс универсальный: одна и та же машинерия обслуживает очередь входящих
обновлений (сейчас) и отдельные очереди для текста и картинок с собственными
размерами пулов (фаза 5, когда появятся сами вызовы к провайдерам).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Состояние очереди для /health и нагрузочной проверки."""

    name: str
    capacity: int
    workers: int
    pending: int
    in_flight: int
    accepted: int
    rejected: int
    failed: int


class JobQueue[T]:
    """Очередь задач фиксированной ёмкости с пулом воркеров."""

    def __init__(
        self,
        name: str,
        handler: Callable[[T], Awaitable[None]],
        *,
        capacity: int,
        workers: int,
    ) -> None:
        if capacity < 1:
            raise ValueError("ёмкость очереди должна быть не меньше 1")
        if workers < 1:
            raise ValueError("в пуле должен быть хотя бы один воркер")

        self._name = name
        self._handler = handler
        self._capacity = capacity
        self._worker_count = workers
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = False
        self._in_flight = 0
        self._accepted = 0
        self._rejected = 0
        self._failed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def accepting(self) -> bool:
        """Принимаем ли новые задачи."""
        return self._accepting

    def start(self) -> None:
        """Поднимает воркеров. Вызывается один раз при старте приложения."""
        if self._workers:
            raise RuntimeError(f"очередь {self._name} уже запущена")
        self._accepting = True
        self._workers = [
            asyncio.create_task(self._worker(), name=f"{self._name}-worker-{index}")
            for index in range(self._worker_count)
        ]

    def submit(self, payload: T) -> bool:
        """Ставит задачу в очередь, не блокируясь.

        Возвращает False, если очередь переполнена или приложение уже
        останавливается. Именно False, а не исключение: переполнение — это
        штатный режим работы под нагрузкой, а не ошибка. Решение, что
        показать пользователю, принимает вызывающий код.
        """
        if not self._accepting:
            self._rejected += 1
            return False
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._rejected += 1
            logger.warning("queue_overflow", queue=self._name, capacity=self._capacity)
            return False
        self._accepted += 1
        return True

    async def join(self, timeout: float) -> bool:  # noqa: ASYNC109
        """Ждёт, пока разберут всё принятое, не останавливая воркеров.

        Возвращает False, если не дождались за отведённое время. В отличие от
        drain, очередь остаётся рабочей — это нужно и тестам, и будущей
        проверке «очередь не растёт» под нагрузкой.
        """
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except TimeoutError:
            return False
        return True

    def stop_accepting(self) -> None:
        """Перестаёт принимать задачи, уже принятые продолжают выполняться."""
        self._accepting = False

    async def drain(self, timeout: float) -> int:  # noqa: ASYNC109
        """Доводит до конца принятые задачи и останавливает воркеров.

        Возвращает число задач, которые не успели: не разобранные из очереди
        плюс прерванные на полпути. Ждать бесконечно нельзя — Railway пришлёт
        SIGKILL по истечении своего окна.
        """
        self._accepting = False
        if not self._workers:
            return self._queue.qsize()

        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except TimeoutError:
            logger.warning("queue_drain_timeout", queue=self._name)

        abandoned = self._queue.qsize() + self._in_flight

        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

        logger.info("queue_drained", queue=self._name, abandoned=abandoned)
        return abandoned

    def stats(self) -> QueueStats:
        """Снимок состояния очереди."""
        return QueueStats(
            name=self._name,
            capacity=self._capacity,
            workers=len(self._workers),
            pending=self._queue.qsize(),
            in_flight=self._in_flight,
            accepted=self._accepted,
            rejected=self._rejected,
            failed=self._failed,
        )

    async def _worker(self) -> None:
        """Тело воркера: берёт задачу и выполняет её до победного."""
        while True:
            payload = await self._queue.get()
            self._in_flight += 1
            try:
                await self._handler(payload)
            except asyncio.CancelledError:
                # Нас гасят по таймауту завершения. task_done вызвать надо,
                # иначе join() у соседнего воркера никогда не вернётся.
                self._in_flight -= 1
                self._queue.task_done()
                raise
            except Exception as error:
                # Упавшая задача не должна ронять воркер: иначе одна кривая
                # входящая полезная нагрузка выбивает из пула по воркеру за раз,
                # и через несколько таких очередь встаёт совсем.
                self._failed += 1
                logger.error("queue_job_failed", queue=self._name, error=repr(error))
                self._in_flight -= 1
                self._queue.task_done()
            else:
                self._in_flight -= 1
                self._queue.task_done()
