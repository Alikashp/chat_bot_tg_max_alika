"""Тесты повторов и предохранителя."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.infra.retry import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryPolicy,
    with_retry,
)


class Sleeper:
    """Записывает задержки вместо того, чтобы ждать."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class TemporaryError(Exception):
    """Временный сбой: повторять можно."""


class PermanentError(Exception):
    """Отказ по существу: повторять бессмысленно."""


def _retryable(error: BaseException) -> bool:
    return isinstance(error, TemporaryError)


# --- Повторы -------------------------------------------------------------


async def test_successful_call_is_not_repeated() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "готово"

    result = await with_retry(operation, policy=RetryPolicy(), is_retryable=_retryable)

    assert result == "готово"
    assert calls == 1


async def test_temporary_failure_is_retried_until_success() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TemporaryError
        return "готово"

    sleeper = Sleeper()
    result = await with_retry(
        operation,
        policy=RetryPolicy(attempts=5),
        is_retryable=_retryable,
        sleep=sleeper,
        random_value=lambda: 0.5,
    )

    assert result == "готово"
    assert calls == 3
    assert len(sleeper.delays) == 2


async def test_permanent_failure_is_not_retried() -> None:
    """Повторять всё подряд — значит показать ошибку втрое позже."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise PermanentError

    sleeper = Sleeper()
    with pytest.raises(PermanentError):
        await with_retry(
            operation,
            policy=RetryPolicy(attempts=5),
            is_retryable=_retryable,
            sleep=sleeper,
        )

    assert calls == 1
    assert sleeper.delays == []


async def test_attempts_are_bounded() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise TemporaryError

    sleeper = Sleeper()
    with pytest.raises(TemporaryError):
        await with_retry(
            operation,
            policy=RetryPolicy(attempts=3),
            is_retryable=_retryable,
            sleep=sleeper,
            random_value=lambda: 0.5,
        )

    assert calls == 3
    assert len(sleeper.delays) == 2


async def test_cancellation_is_never_retried() -> None:
    """Отмена — это остановка сервиса, а не сбой провайдера."""
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_retry(
            operation,
            policy=RetryPolicy(attempts=5),
            is_retryable=lambda _: True,
            sleep=Sleeper(),
        )

    assert calls == 1


def test_delay_grows_exponentially() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=0.0)

    delays = [policy.delay_for(attempt, 0.5) for attempt in (1, 2, 3, 4)]

    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_delay_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0, jitter=0.0)

    assert policy.delay_for(10, 0.5) == 5.0


def test_jitter_spreads_the_delay() -> None:
    """Без разброса все клиенты вернутся одновременно и добьют провайдер."""
    policy = RetryPolicy(base_delay=10.0, max_delay=100.0, jitter=0.5)

    earliest = policy.delay_for(1, 0.0)
    latest = policy.delay_for(1, 0.999)

    assert earliest == pytest.approx(5.0)
    assert latest == pytest.approx(15.0, abs=0.01)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempts": 0},
        {"base_delay": 0},
        {"base_delay": 10, "max_delay": 1},
        {"jitter": 1.5},
        {"jitter": -0.1},
    ],
)
def test_invalid_policy_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# --- Предохранитель ------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _fail(breaker: CircuitBreaker) -> None:
    async def operation() -> None:
        raise TemporaryError

    with pytest.raises(TemporaryError):
        await breaker.call(operation)


async def test_healthy_provider_keeps_the_circuit_closed() -> None:
    breaker = CircuitBreaker("test", failure_threshold=2)

    async def operation() -> str:
        return "готово"

    assert await breaker.call(operation) == "готово"
    assert breaker.state is CircuitState.CLOSED


async def test_circuit_opens_after_threshold() -> None:
    breaker = CircuitBreaker("test", failure_threshold=3)

    for _ in range(3):
        await _fail(breaker)

    assert breaker.state is CircuitState.OPEN


async def test_open_circuit_fails_fast_without_calling_provider() -> None:
    """В этом весь смысл: лёгший провайдер не должен копить очередь."""
    breaker = CircuitBreaker("test", failure_threshold=1)
    await _fail(breaker)
    called = False

    async def operation() -> None:
        nonlocal called
        called = True

    with pytest.raises(CircuitOpenError):
        await breaker.call(operation)

    assert called is False


async def test_success_resets_the_failure_count() -> None:
    """Разрозненные сбои не должны накапливаться до размыкания."""
    breaker = CircuitBreaker("test", failure_threshold=3)

    async def ok() -> None:
        return None

    await _fail(breaker)
    await _fail(breaker)
    await breaker.call(ok)
    await _fail(breaker)
    await _fail(breaker)

    assert breaker.state is CircuitState.CLOSED


async def test_circuit_half_opens_after_recovery_time() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "test", failure_threshold=1, recovery_seconds=30, clock=clock
    )
    await _fail(breaker)

    clock.advance(30)

    assert breaker.state is CircuitState.HALF_OPEN


async def test_successful_probe_closes_the_circuit() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "test", failure_threshold=1, recovery_seconds=30, clock=clock
    )
    await _fail(breaker)
    clock.advance(30)

    async def ok() -> str:
        return "жив"

    assert await breaker.call(ok) == "жив"
    assert breaker.state is CircuitState.CLOSED


async def test_failed_probe_opens_the_circuit_again() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        "test", failure_threshold=1, recovery_seconds=30, clock=clock
    )
    await _fail(breaker)
    clock.advance(30)

    await _fail(breaker)

    assert breaker.state is CircuitState.OPEN
    clock.advance(29)
    assert breaker.state is CircuitState.OPEN


async def test_only_one_probe_passes_at_a_time() -> None:
    """Иначе «проверка» превращается в повторный залп по провайдеру."""
    clock = FakeClock()
    breaker = CircuitBreaker(
        "test", failure_threshold=1, recovery_seconds=30, clock=clock
    )
    await _fail(breaker)
    clock.advance(30)

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    probe = asyncio.create_task(breaker.call(slow))
    await started.wait()

    with pytest.raises(CircuitOpenError):
        await breaker.call(slow)

    release.set()
    await probe
    assert calls == 1


async def test_request_error_does_not_open_the_circuit() -> None:
    """Провайдер жив, ерунду прислали мы — размыкать цепь неправильно."""
    breaker = CircuitBreaker("test", failure_threshold=1)

    async def operation() -> None:
        raise PermanentError

    with pytest.raises(PermanentError):
        await breaker.call(operation, is_failure=_retryable)

    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("kwargs", [{"failure_threshold": 0}, {"recovery_seconds": 0}])
def test_invalid_breaker_configuration_is_rejected(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker("test", **kwargs)
