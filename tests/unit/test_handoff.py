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
    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))

    assert handoff.take(token) == Carried(topic="Фотосинтез")
    assert handoff.take(token) is None
    assert handoff.was_taken(token)


def test_peek_does_not_take() -> None:
    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))

    assert handoff.peek(token) is not None
    assert handoff.take(token) is not None


def test_a_failed_job_gives_the_token_back() -> None:
    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    carried = Carried(topic="Фотосинтез", material="текст")
    token = handoff.put(carried)
    handoff.take(token)

    handoff.give_back(token, carried)

    assert handoff.take(token) == carried


def test_a_token_expires_and_is_not_mistaken_for_used() -> None:
    """Устаревший жетон — «данных нет», а не «уже сделано»."""
    clock = Clock()
    handoff: MemoryHandoff[Carried] = MemoryHandoff(ttl_seconds=60, clock=clock)
    token = handoff.put(Carried(topic="Фотосинтез"))

    clock.now = 61
    assert handoff.take(token) is None
    assert not handoff.was_taken(token)


def test_an_unknown_token_is_neither_alive_nor_used() -> None:
    handoff: MemoryHandoff[Carried] = MemoryHandoff()

    assert handoff.peek("нет-такого") is None
    assert not handoff.was_taken("нет-такого")


def test_memory_is_bounded() -> None:
    """Ненажатые жетоны не копятся до конца жизни процесса."""
    handoff: MemoryHandoff[Carried] = MemoryHandoff(max_items=3)
    tokens = [handoff.put(Carried(topic=f"тема {n}")) for n in range(5)]

    assert handoff.peek(tokens[0]) is None
    assert handoff.peek(tokens[-1]) is not None


def test_the_default_store_is_bounded_too() -> None:
    """Ф11-0г: без настроек хранилище тоже ограничено — тысячей жетонов."""
    from app.infra.handoff import MAX_ITEMS

    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    tokens = [handoff.put(Carried(topic=f"тема {n}")) for n in range(MAX_ITEMS + 5)]

    assert handoff.peek(tokens[0]) is None
    assert handoff.peek(tokens[-1]) is not None
    assert len(handoff._entries) == MAX_ITEMS


def test_an_update_replaces_what_lies_under_a_live_token() -> None:
    """Экран параметров меняет черновик под тем же жетоном."""
    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))

    assert handoff.update(token, Carried(topic="Дыхание растений"))
    assert handoff.peek(token) == Carried(topic="Дыхание растений")


def test_nothing_is_updated_under_a_taken_or_unknown_token() -> None:
    handoff: MemoryHandoff[Carried] = MemoryHandoff()
    token = handoff.put(Carried(topic="Фотосинтез"))
    handoff.take(token)

    assert not handoff.update(token, Carried(topic="Другая"))
    assert not handoff.update("нет-такого", Carried(topic="Другая"))
    assert handoff.peek(token) is None


def test_the_weight_limit_pushes_the_oldest_out() -> None:
    """Под черновиком бывает файл в 20 МБ — память ограничена и объёмом."""
    handoff: MemoryHandoff[Carried] = MemoryHandoff(
        weigh=lambda carried: len(carried.material), max_weight=10
    )
    old = handoff.put(Carried(topic="Старый", material="x" * 6))
    new = handoff.put(Carried(topic="Новый", material="y" * 6))

    assert handoff.peek(old) is None
    assert handoff.peek(new) is not None


def test_an_updated_entry_is_the_freshest() -> None:
    """Обновлённый черновик вытесняется последним — им сейчас пользуются."""
    handoff: MemoryHandoff[Carried] = MemoryHandoff(max_items=2)
    first = handoff.put(Carried(topic="Первый"))
    second = handoff.put(Carried(topic="Второй"))
    handoff.update(first, Carried(topic="Первый, исправленный"))

    handoff.put(Carried(topic="Третий"))

    assert handoff.peek(first) is not None
    assert handoff.peek(second) is None
