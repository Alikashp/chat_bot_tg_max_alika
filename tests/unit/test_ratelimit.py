"""Тесты token bucket. Время управляемое: тесты не ждут по-настоящему."""

from __future__ import annotations

import pytest

from app.infra.ratelimit import TokenBucket


class FakeTime:
    """Часы и sleep, связанные между собой.

    Ожидание не тратит реального времени, но двигает часы — иначе проверить
    восстановление токенов можно было бы только настоящими паузами, и набор
    тестов стал бы медленным и хлипким.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_burst_passes_without_waiting() -> None:
    """Короткий всплеск проходит сразу — в этом смысл burst."""
    fake = FakeTime()
    bucket = TokenBucket(rate=1, burst=5, clock=fake.clock, sleep=fake.sleep)

    for _ in range(5):
        await bucket.acquire()

    assert fake.slept == []


async def test_call_beyond_burst_waits() -> None:
    fake = FakeTime()
    bucket = TokenBucket(rate=2, burst=2, clock=fake.clock, sleep=fake.sleep)
    await bucket.acquire()
    await bucket.acquire()

    await bucket.acquire()

    assert fake.slept == [pytest.approx(0.5)]


async def test_tokens_refill_over_time() -> None:
    fake = FakeTime()
    bucket = TokenBucket(rate=10, burst=10, clock=fake.clock, sleep=fake.sleep)
    for _ in range(10):
        await bucket.acquire()
    assert bucket.available == pytest.approx(0)

    fake.now += 1.0
    await bucket.acquire()

    assert fake.slept == []


async def test_bucket_never_overflows() -> None:
    """За час простоя не должно накопиться на час работы вперёд."""
    fake = FakeTime()
    bucket = TokenBucket(rate=1, burst=3, clock=fake.clock, sleep=fake.sleep)

    fake.now += 3600

    await bucket.acquire(3)
    assert bucket.available == pytest.approx(0)


async def test_sustained_rate_is_limited() -> None:
    """Двадцать вызовов при rate=10 и burst=10 занимают около секунды."""
    fake = FakeTime()
    bucket = TokenBucket(rate=10, burst=10, clock=fake.clock, sleep=fake.sleep)

    for _ in range(20):
        await bucket.acquire()

    assert fake.now == pytest.approx(1.0, abs=0.001)


async def test_asking_for_more_than_burst_is_rejected() -> None:
    """Иначе вызов ждал бы вечно, а выглядело бы это как зависание."""
    bucket = TokenBucket(rate=1, burst=2)

    with pytest.raises(ValueError):
        await bucket.acquire(3)


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (-1, 1), (1, 0)])
def test_invalid_configuration_is_rejected(rate: float, burst: int) -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=rate, burst=burst)
