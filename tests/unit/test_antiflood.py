"""Тесты ограничения одновременных задач на пользователя."""

from __future__ import annotations

import pytest

from app.infra.antiflood import FloodGuard, FloodLimitExceededError


def test_first_task_is_allowed() -> None:
    guard = FloodGuard(limit=1)

    assert guard.try_acquire("user-1") is True


def test_second_task_of_the_same_user_is_refused() -> None:
    guard = FloodGuard(limit=1)
    guard.try_acquire("user-1")

    assert guard.try_acquire("user-1") is False


def test_other_users_are_not_affected() -> None:
    """Один активный пользователь не должен мешать остальным."""
    guard = FloodGuard(limit=1)
    guard.try_acquire("user-1")

    assert guard.try_acquire("user-2") is True


def test_slot_frees_up_after_release() -> None:
    guard = FloodGuard(limit=1)
    guard.try_acquire("user-1")

    guard.release("user-1")

    assert guard.try_acquire("user-1") is True


def test_limit_above_one_is_respected() -> None:
    guard = FloodGuard(limit=2)

    assert guard.try_acquire("user-1") is True
    assert guard.try_acquire("user-1") is True
    assert guard.try_acquire("user-1") is False


def test_context_manager_holds_and_frees_the_slot() -> None:
    guard = FloodGuard(limit=1)

    with guard.slot("user-1"):
        assert guard.active("user-1") == 1

    assert guard.active("user-1") == 0


def test_context_manager_refuses_when_full() -> None:
    guard = FloodGuard(limit=1)

    with (
        guard.slot("user-1"),
        pytest.raises(FloodLimitExceededError),
        guard.slot("user-1"),
    ):
        pass


def test_slot_is_freed_even_if_the_task_fails() -> None:
    """Иначе один сбой навсегда запирает пользователя."""
    guard = FloodGuard(limit=1)

    with pytest.raises(RuntimeError), guard.slot("user-1"):
        raise RuntimeError("провал")

    assert guard.try_acquire("user-1") is True


def test_finished_users_are_forgotten() -> None:
    """Словарь учёта не должен расти по числу пользователей за всё время."""
    guard = FloodGuard(limit=1)

    for index in range(1000):
        key = f"user-{index}"
        guard.try_acquire(key)
        guard.release(key)

    assert guard.tracked_keys == 0


def test_extra_release_does_not_break_the_counter() -> None:
    guard = FloodGuard(limit=1)

    guard.release("нет-такого")

    assert guard.tracked_keys == 0
    assert guard.try_acquire("нет-такого") is True


def test_invalid_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        FloodGuard(limit=0)
