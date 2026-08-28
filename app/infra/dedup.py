"""Дедупликация обновлений.

Telegram повторяет доставку вебхука, если не получил быстрый 200 OK, а в MAX
сквозного идентификатора обновления нет вовсе, и ключ приходится собирать из
полей события (docs/research.md §1.4). В обоих случаях одно и то же обновление
может прийти дважды.

Для ответа «понг» повтор безвреден. Для картинки — это вторая отрисовка:
лишние деньги провайдеру и второй списанный лимит у пользователя.

Память ограничена жёстко: и по времени жизни ключа, и по их количеству.
Кэш дедупликации, который растёт без предела, — это утечка, которая проявится
через неделю аптайма, а не на тестах.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class Deduplicator:
    """Кэш уже виденных ключей обновлений с TTL и ограничением размера."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_keys: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds должен быть больше нуля")
        if max_keys < 1:
            raise ValueError("max_keys должен быть не меньше 1")

        self._ttl = ttl_seconds
        self._max_keys = max_keys
        self._clock = clock
        #: ключ -> момент, после которого запись считается протухшей.
        #: OrderedDict, потому что вставки идут по возрастанию срока годности,
        #: и протухшие всегда лежат в начале — их чистка выходит амортизированно
        #: постоянной, без обхода всего кэша.
        self._seen: OrderedDict[str, float] = OrderedDict()

    def is_new(self, key: str) -> bool:
        """Проверяет и одновременно запоминает ключ.

        Возвращает True, если такое обновление встретилось впервые и его надо
        обработать, и False, если это повтор.

        Проверка и запись — одно действие: между ними нет await, поэтому две
        параллельные корутины не могут обе получить True на один ключ.
        """
        now = self._clock()
        self._purge_expired(now)

        if key in self._seen:
            return False

        if len(self._seen) >= self._max_keys:
            # Кэш забит живыми ключами: вытесняем самый старый. Теоретически
            # это может пропустить повтор, но переполнение памяти хуже.
            self._seen.popitem(last=False)

        self._seen[key] = now + self._ttl
        return True

    def __len__(self) -> int:
        return len(self._seen)

    def _purge_expired(self, now: float) -> None:
        while self._seen:
            key, expires_at = next(iter(self._seen.items()))
            if expires_at > now:
                return
            self._seen.pop(key)
