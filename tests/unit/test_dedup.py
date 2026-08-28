"""Тесты дедупликации: повторы, TTL и ограничение памяти."""

from __future__ import annotations

import pytest

from app.infra.dedup import Deduplicator


class FakeClock:
    """Управляемое время: тесты не должны ничего ждать по-настоящему."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_first_key_is_new() -> None:
    dedup = Deduplicator(ttl_seconds=60, max_keys=100)

    assert dedup.is_new("update-1") is True


def test_repeated_key_is_not_new() -> None:
    """Повторная доставка того же обновления не должна обрабатываться."""
    dedup = Deduplicator(ttl_seconds=60, max_keys=100)
    dedup.is_new("update-1")

    assert dedup.is_new("update-1") is False


def test_different_keys_are_independent() -> None:
    dedup = Deduplicator(ttl_seconds=60, max_keys=100)

    assert dedup.is_new("a") is True
    assert dedup.is_new("b") is True


def test_key_is_forgotten_after_ttl() -> None:
    clock = FakeClock()
    dedup = Deduplicator(ttl_seconds=60, max_keys=100, clock=clock)
    dedup.is_new("update-1")

    clock.advance(61)

    assert dedup.is_new("update-1") is True


def test_key_is_still_remembered_just_before_ttl() -> None:
    clock = FakeClock()
    dedup = Deduplicator(ttl_seconds=60, max_keys=100, clock=clock)
    dedup.is_new("update-1")

    clock.advance(59)

    assert dedup.is_new("update-1") is False


def test_expired_keys_free_memory() -> None:
    """Иначе кэш растёт, пока хватает памяти."""
    clock = FakeClock()
    dedup = Deduplicator(ttl_seconds=10, max_keys=1000, clock=clock)
    for index in range(100):
        dedup.is_new(f"key-{index}")
    assert len(dedup) == 100

    clock.advance(11)
    dedup.is_new("свежий")

    assert len(dedup) == 1


def test_size_never_exceeds_the_limit() -> None:
    """Даже если ключи живые: переполнение памяти хуже пропущенного повтора."""
    dedup = Deduplicator(ttl_seconds=3600, max_keys=10)

    for index in range(100):
        dedup.is_new(f"key-{index}")

    assert len(dedup) == 10


def test_oldest_key_is_evicted_first() -> None:
    dedup = Deduplicator(ttl_seconds=3600, max_keys=2)
    dedup.is_new("первый")
    dedup.is_new("второй")

    dedup.is_new("третий")

    # Порядок проверок важен: is_new не только спрашивает, но и запоминает.
    # Сначала убеждаемся, что более новый ключ на месте, и только потом — что
    # самый старый вытеснен, иначе первая же проверка вытеснит второй ключ.
    assert dedup.is_new("второй") is False
    assert dedup.is_new("первый") is True


@pytest.mark.parametrize(("ttl", "max_keys"), [(0, 10), (-1, 10), (60, 0)])
def test_invalid_configuration_is_rejected(ttl: float, max_keys: int) -> None:
    with pytest.raises(ValueError):
        Deduplicator(ttl_seconds=ttl, max_keys=max_keys)
