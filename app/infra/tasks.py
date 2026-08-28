"""Фоновые задачи и их корректное завершение.

Обработчик вебхука обязан ответить 200 OK быстрее 50 мс (§3.4.1), поэтому
сама работа уходит в фоновую задачу. Задачи нужно где-то держать: если не
хранить ссылку, сборщик мусора может убить работающую корутину на середине.

На фазе 2 этот класс становится основой пула воркеров: ограниченная очередь
и backpressure добавляются поверх, а учёт задач и graceful shutdown остаются
теми же.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.infra.logging import get_logger

logger = get_logger(__name__)


class BackgroundTasks:
    """Набор выполняющихся фоновых задач."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._accepting = True

    @property
    def running(self) -> int:
        """Сколько задач выполняется прямо сейчас."""
        return len(self._tasks)

    @property
    def accepting(self) -> bool:
        """Принимаем ли новые задачи (после SIGTERM — нет)."""
        return self._accepting

    def spawn(self, coro: Coroutine[Any, Any, None]) -> bool:
        """Запускает задачу в фоне.

        Возвращает False, если приложение уже завершается: тогда корутина
        закрывается, не начав работу, — иначе Python ругнётся на
        «coroutine was never awaited».
        """
        if not self._accepting:
            coro.close()
            return False

        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_failure)
        return True

    def stop_accepting(self) -> None:
        """Перестаёт принимать новые задачи."""
        self._accepting = False

    async def drain(self, timeout: float) -> int:  # noqa: ASYNC109
        """Ждёт завершения текущих задач.

        Возвращает число задач, которые не успели и были сняты. Ждать
        бесконечно нельзя: Railway пришлёт SIGKILL по истечении своего окна,
        и тогда мы не успеем даже записать лог.

        Здесь именно параметр ``timeout``, а не ``asyncio.timeout`` снаружи,
        как советует ruff: нам нужно не просто прерваться по истечении
        времени, а узнать, сколько задач не успело, и снять их поимённо.
        """
        if not self._tasks:
            return 0

        pending = set(self._tasks)
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        logger.info(
            "background_tasks_drained",
            finished=len(done),
            abandoned=len(still_pending),
        )

        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

        return len(still_pending)

    @staticmethod
    def _log_failure(task: asyncio.Task[None]) -> None:
        """Логирует упавшую задачу.

        Без этого исключение в фоновой задаче тихо теряется: возвращать его
        некому, потому что задачу никто не ожидает.
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("background_task_failed", error=repr(error))
