"""Ограничение частоты исходящих вызовов (token bucket).

Нужно на каждого внешнего провайдера отдельно (§3.4.4). Смысл двойной:
не ловить 429 от провайдера и не сжечь бюджет за час, если что-то пойдёт не
так — например, цикл ретраев зациклится или бот попадёт в рассылку.

Ограничиваем именно исходящие вызовы, а не входящие запросы: входящие
ограничивает ёмкость очереди.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

#: Допуск при сравнении накопленных токенов.
#:
#: Токены — число с плавающей точкой, и после серии пополнений вида
#: 0.1 + 0.1 + ... в ведре оказывается 0.9999999999999999 вместо ровно 1.0.
#: Без допуска вызов решает, что токена не хватает, просит подождать
#: ничтожную долю секунды, просыпается с тем же результатом — и так по кругу.
#: На управляемых часах это вечный цикл, на реальных — холостая прокрутка,
#: жгущая процессор, пока часы не уйдут достаточно далеко.
_EPSILON = 1e-9


class TokenBucket:
    """Классический token bucket.

    В ведро с постоянной скоростью ``rate`` капают токены, но не больше
    ``burst`` штук. Каждый вызов забирает токен; если токенов нет — ждёт.

    Именно ``burst`` отличает эту схему от «не больше N в секунду»: короткий
    всплеск проходит сразу, а длительная нагрузка выравнивается до ``rate``.
    """

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate должен быть больше нуля")
        if burst < 1:
            raise ValueError("burst должен быть не меньше 1")

        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._tokens = float(burst)
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    @property
    def available(self) -> float:
        """Сколько токенов доступно прямо сейчас (для тестов и диагностики)."""
        return self._tokens

    async def acquire(self, tokens: int = 1) -> None:
        """Забирает токены, при необходимости дожидаясь их появления."""
        if tokens < 1:
            raise ValueError("забирать нужно хотя бы один токен")
        if tokens > self._burst:
            raise ValueError(f"за раз нельзя забрать больше {self._burst} токенов")

        while True:
            async with self._lock:
                self._refill()
                if self._tokens + _EPSILON >= tokens:
                    self._tokens -= tokens
                    return
                wait_for = (tokens - self._tokens) / self._rate
            # Спим вне блокировки: иначе один ждущий вызов застопорит всех,
            # включая те, которым токенов уже хватило бы.
            await self._sleep(wait_for)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated_at
        if elapsed <= 0:
            return
        self._updated_at = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
