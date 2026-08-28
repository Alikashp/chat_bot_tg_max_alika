"""Повторы и предохранитель для вызовов к внешним провайдерам.

Два разных механизма, которые важно не путать:

* **Повторы** лечат единичный сбой — сеть моргнула, провайдер вернул 503.
  Применимы только к идемпотентным вызовам: повторить запрос за ответом
  можно, повторить списание денег — нет. Проверить идемпотентность за
  вызывающий код невозможно, поэтому это его ответственность.

* **Предохранитель** лечит затяжной сбой. Когда провайдер лёг, повторы делают
  только хуже: очередь копится, пользователи ждут, счёт растёт. Предохранитель
  размыкает цепь и отвечает отказом сразу, а через заданное время пробует один
  раз — не ожил ли.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from app.infra.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Параметры повторов."""

    #: Сколько всего попыток, включая первую.
    attempts: int = 3
    #: Задержка перед вторым заходом; дальше удваивается.
    base_delay: float = 0.5
    #: Потолок задержки, чтобы ожидание не выросло до минут.
    max_delay: float = 8.0
    #: Доля случайного разброса, 0.0-1.0.
    #:
    #: Без разброса все клиенты, отвалившиеся в одну секунду, вернутся тоже
    #: в одну секунду и добьют провайдер, который только начал подниматься.
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts должен быть не меньше 1")
        if self.base_delay <= 0:
            raise ValueError("base_delay должен быть больше нуля")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay не может быть меньше base_delay")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter должен быть в диапазоне от 0 до 1")

    def delay_for(self, attempt: int, random_value: float) -> float:
        """Задержка перед попыткой номер ``attempt`` (нумерация с 1).

        ``random_value`` — число из [0, 1). Передаётся снаружи, чтобы
        поведение можно было проверить тестом, а не наблюдать.
        """
        exponential: float = min(
            self.base_delay * float(2 ** (attempt - 1)), self.max_delay
        )
        spread = exponential * self.jitter
        return exponential - spread + spread * 2.0 * random_value


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[BaseException], bool],
    sleep: Callable[[float], Awaitable[None]] | None = None,
    random_value: Callable[[], float] | None = None,
    name: str = "operation",
) -> T:
    """Выполняет операцию, повторяя её при временных сбоях.

    ``is_retryable`` решает, что считать временным сбоем. Повторять всё
    подряд нельзя: ошибка в запросе повторится ровно так же, а сообщение об
    ошибке пользователь получит втрое позже.
    """
    do_sleep = sleep if sleep is not None else asyncio.sleep
    # Разброс задержки — не криптография, обычного random достаточно.
    next_random = random_value if random_value is not None else random.random

    last_error: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            # Нас гасят при остановке сервиса. Повторять нельзя: это не сбой.
            raise
        except Exception as error:
            if not is_retryable(error):
                raise
            last_error = error
            if attempt == policy.attempts:
                break
            delay = policy.delay_for(attempt, next_random())
            logger.warning(
                "retrying",
                operation=name,
                attempt=attempt,
                delay=round(delay, 3),
                error=repr(error),
            )
            await do_sleep(delay)

    if last_error is None:  # pragma: no cover — сюда можно попасть только после сбоя
        raise RuntimeError("повторы завершились без ошибки и без результата")
    raise last_error


class CircuitState(StrEnum):
    """Состояние предохранителя."""

    #: Цепь замкнута: вызовы проходят.
    CLOSED = "closed"
    #: Цепь разомкнута: вызовы отвергаются сразу.
    OPEN = "open"
    #: Пробный режим: пропускаем один вызов и смотрим на результат.
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Провайдер признан нерабочим, вызов не делался."""


class CircuitBreaker:
    """Предохранитель на одного провайдера."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold должен быть не меньше 1")
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds должен быть больше нуля")

        self._name = name
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        #: В полуоткрытом состоянии пробный вызов должен быть ровно один:
        #: если пустить всех, «проверка» превратится в повторный залп по
        #: провайдеру, который ещё не встал на ноги.
        self._probe_taken = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        """Текущее состояние с учётом истёкшего времени восстановления."""
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self._recovery
        ):
            return CircuitState.HALF_OPEN
        return self._state

    async def call[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        is_failure: Callable[[BaseException], bool] | None = None,
    ) -> T:
        """Выполняет операцию, если цепь позволяет.

        ``is_failure`` отделяет неполадки провайдера от отказов по существу.
        Ошибка в самом запросе не должна размыкать цепь: провайдер жив, это
        мы прислали ерунду.
        """
        counts_as_failure = is_failure if is_failure is not None else _always
        self._enter()

        try:
            result = await operation()
        except asyncio.CancelledError:
            # Остановка сервиса — не приговор провайдеру.
            self._release_probe()
            raise
        except Exception as error:
            if counts_as_failure(error):
                self._record_failure()
            else:
                self._release_probe()
            raise

        self._record_success()
        return result

    def _enter(self) -> None:
        state = self.state
        if state is CircuitState.OPEN:
            raise CircuitOpenError(f"провайдер {self._name} недоступен")
        if state is CircuitState.HALF_OPEN:
            if self._probe_taken:
                raise CircuitOpenError(
                    f"провайдер {self._name} проверяется, попробуйте позже"
                )
            self._state = CircuitState.HALF_OPEN
            self._probe_taken = True

    def _release_probe(self) -> None:
        """Возвращает право на пробный вызов, не меняя состояния."""
        if self._state is CircuitState.HALF_OPEN:
            self._probe_taken = False

    def _record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            logger.info("circuit_closed", provider=self._name)
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._probe_taken = False

    def _record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            # Пробный вызов не удался: размыкаем снова, отсчёт с нуля.
            self._open()
            return

        self._failures += 1
        if self._failures >= self._threshold:
            self._open()

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._probe_taken = False
        logger.warning("circuit_opened", provider=self._name, failures=self._failures)


def _always(_error: BaseException) -> bool:
    return True
