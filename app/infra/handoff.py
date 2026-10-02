"""Жетоны кнопок-связок в памяти процесса (порт Handoff).

Память, а не база, — намеренно: под жетоном лежит текст доклада, а ему в базе
не место. Цена — после перезапуска старые кнопки данных не находят и честно
об этом говорят.

Память ограничена и по времени, и по числу: жетоны, которые никто не нажал,
иначе копились бы до конца жизни процесса.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from app.ports.handoff import Carried

#: Сколько живёт жетон. Доклад по презентации просят сразу или в тот же
#: вечер, а не через неделю.
TTL_SECONDS = 6 * 60 * 60

#: Сколько жетонов держим. Под жетоном бывает текст доклада до 40 000 знаков,
#: и тысяча таких — около сотни мегабайт в худшем случае.
MAX_ITEMS = 1_000


@dataclass(slots=True)
class _Entry:
    expires_at: float
    #: None — жетон уже взяли. Запись остаётся, чтобы отличить «уже сделано»
    #: от «устарело».
    carried: Carried | None


class MemoryHandoff:
    """Реализация порта Handoff."""

    def __init__(
        self,
        *,
        ttl_seconds: float = TTL_SECONDS,
        max_items: int = MAX_ITEMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_items
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def put(self, carried: Carried) -> str:
        self._forget_stale()
        token = secrets.token_hex(8)
        self._entries[token] = _Entry(self._clock() + self._ttl, carried)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return token

    def peek(self, token: str) -> Carried | None:
        entry = self._alive(token)
        return entry.carried if entry is not None else None

    def take(self, token: str) -> Carried | None:
        entry = self._alive(token)
        if entry is None or entry.carried is None:
            return None
        carried, entry.carried = entry.carried, None
        return carried

    def was_taken(self, token: str) -> bool:
        entry = self._alive(token)
        return entry is not None and entry.carried is None

    def give_back(self, token: str, carried: Carried) -> None:
        entry = self._alive(token)
        if entry is not None:
            entry.carried = carried
            return
        self._entries[token] = _Entry(self._clock() + self._ttl, carried)

    def _alive(self, token: str) -> _Entry | None:
        entry = self._entries.get(token)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry

    def _forget_stale(self) -> None:
        now = self._clock()
        while self._entries:
            token, entry = next(iter(self._entries.items()))
            if entry.expires_at > now:
                return
            del self._entries[token]
