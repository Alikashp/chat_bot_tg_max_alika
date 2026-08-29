"""Обвязка вокруг вызова к провайдеру.

Здесь наконец подключается всё, что было написано и покрыто тестами на фазе 2,
но до сих пор ни к чему не крепилось: ограничитель частоты, повторы с
разбросом задержки и предохранитель. Порядок слоёв важен и выбран так:

    предохранитель → повторы → ограничитель частоты → сам вызов

Предохранитель снаружи: когда провайдер лёг, отказ должен приходить мгновенно,
не тратя ни повторов, ни токенов ведра. Ограничитель внутри повторов: каждая
попытка — отдельный вызов к провайдеру, и каждая обязана уложиться в его
квоту, иначе цикл повторов сам себе устроит 429.

Ограничение одновременных вызовов задаётся отдельно для текста и для картинок:
картинка занимает провайдера пятнадцать секунд, сообщение — секунду, и мерить
их одной меркой неправильно.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.adapters.ai.errors import ProviderTimeoutError, ProviderUnavailableError
from app.infra.ratelimit import TokenBucket
from app.infra.retry import CircuitBreaker, RetryPolicy, with_retry


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Как обходиться с конкретным провайдером."""

    #: Разрешённых вызовов в секунду.
    rate: float
    #: Насколько допустим короткий всплеск сверх rate.
    burst: int
    #: Сколько вызовов к этому провайдеру может идти одновременно.
    concurrency: int
    #: Повторы. Для картинок число попыток обычно меньше — они дороже.
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    #: Сколько подряд неудач размыкают цепь.
    failure_threshold: int = 5
    #: Через сколько секунд пробуем, не ожил ли провайдер.
    recovery_seconds: float = 30.0
    #: Повторять ли при таймауте.
    #:
    #: Для текста — да. Для картинок — нет: таймаут не означает, что запрос не
    #: выполнился на той стороне, и повтор рискует оплатить одну картинку
    #: дважды. Пользователь в любом случае ничего не теряет — лимит
    #: списывается только за доставленный результат.
    retry_on_timeout: bool = True


class ResilientCaller:
    """Один провайдер под защитой ограничителя, повторов и предохранителя."""

    def __init__(self, name: str, policy: ProviderPolicy) -> None:
        self._name = name
        self._policy = policy
        self._bucket = TokenBucket(rate=policy.rate, burst=policy.burst)
        self._breaker = CircuitBreaker(
            name,
            failure_threshold=policy.failure_threshold,
            recovery_seconds=policy.recovery_seconds,
        )
        self._slots = asyncio.Semaphore(policy.concurrency)

    @property
    def name(self) -> str:
        return self._name

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Выполняет вызов со всей обвязкой."""

        async def guarded() -> T:
            await self._bucket.acquire()
            async with self._slots:
                return await operation()

        async def with_retries() -> T:
            return await with_retry(
                guarded,
                policy=self._policy.retry,
                is_retryable=self._is_retryable,
                name=self._name,
            )

        return await self._breaker.call(with_retries, is_failure=self._counts_as_outage)

    def _is_retryable(self, error: BaseException) -> bool:
        """Повторяем только временные сбои провайдера."""
        if isinstance(error, ProviderTimeoutError):
            return self._policy.retry_on_timeout
        return isinstance(error, ProviderUnavailableError)

    @staticmethod
    def _counts_as_outage(error: BaseException) -> bool:
        """Размыкаем цепь только из-за самого провайдера.

        Отказ по существу — 4xx — не признак того, что провайдер лёг, и
        обрывать из-за него доступ остальным пользователям неправильно.
        """
        return isinstance(error, ProviderUnavailableError)
