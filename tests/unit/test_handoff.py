"""Жетоны кнопок-связок: забираются один раз, живут ограниченно (Д3)."""

from __future__ import annotations

from app.infra.handoff import MemoryHandoff
from app.ports.handoff import Carried


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_token_is_taken_once() -> None:
    handoff = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))

    assert handoff.take(token) == Carried(topic="Фотосинтез")
    assert handoff.take(token) is None
    assert handoff.was_taken(token)


def test_peek_does_not_take() -> None:
    handoff = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))

    assert handoff.peek(token) is not None
    assert handoff.take(token) is not None


def test_a_failed_job_gives_the_token_back() -> None:
    handoff = MemoryHandoff()
    carried = Carried(topic="Фотосинтез", material="текст")
    token = handoff.put(carried)
    handoff.take(token)

    handoff.give_back(token, carried)

    assert handoff.take(token) == carried


def test_a_token_expires_and_is_not_mistaken_for_used() -> None:
    """Устаревший жетон — «данных нет», а не «уже сделано»."""
    clock = Clock()
    handoff = MemoryHandoff(ttl_seconds=60, clock=clock)
    token = handoff.put(Carried(topic="Фотосинтез"))

    clock.now = 61
    assert handoff.take(token) is None
    assert not handoff.was_taken(token)


def test_an_unknown_token_is_neither_alive_nor_used() -> None:
    handoff = MemoryHandoff()

    assert handoff.peek("нет-такого") is None
    assert not handoff.was_taken("нет-такого")


def test_memory_is_bounded() -> None:
    """Ненажатые жетоны не копятся до конца жизни процесса."""
    handoff = MemoryHandoff(max_items=3)
    tokens = [handoff.put(Carried(topic=f"тема {n}")) for n in range(5)]

    assert handoff.peek(tokens[0]) is None
    assert handoff.peek(tokens[-1]) is not None
