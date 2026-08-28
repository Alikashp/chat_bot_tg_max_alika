"""Ограничение одновременных задач на одного пользователя.

Без него один пользователь, который зажал кнопку «Ещё раз», занимает весь пул
воркеров, и остальные ждут. Пул общий, а значит его надо защищать не только от
общей нагрузки (это делает ёмкость очереди), но и от одного слишком активного
источника.

По умолчанию — одна задача на вид работы: одна картинка и одно сообщение
одновременно (§3.4.8). Это не наказание, а честное «дождись предыдущего»:
человек всё равно не может осмысленно читать два ответа сразу.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class FloodLimitExceededError(RuntimeError):
    """У пользователя уже выполняется столько задач, сколько разрешено."""


class FloodGuard:
    """Считает одновременные задачи по ключу."""

    def __init__(self, *, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit должен быть не меньше 1")
        self._limit = limit
        self._active: dict[str, int] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def active(self, key: str) -> int:
        """Сколько задач по ключу выполняется прямо сейчас."""
        return self._active.get(key, 0)

    @property
    def tracked_keys(self) -> int:
        """Сколько ключей сейчас в учёте.

        Нужно тестам: словарь обязан пустеть, иначе это утечка, растущая
        со скоростью притока новых пользователей.
        """
        return len(self._active)

    def try_acquire(self, key: str) -> bool:
        """Занимает слот. Возвращает False, если свободных нет."""
        current = self._active.get(key, 0)
        if current >= self._limit:
            return False
        self._active[key] = current + 1
        return True

    def release(self, key: str) -> None:
        """Освобождает слот."""
        current = self._active.get(key, 0)
        if current <= 1:
            # Удаляем ключ целиком, а не оставляем ноль: иначе словарь растёт
            # по числу пользователей за всё время работы процесса.
            self._active.pop(key, None)
            return
        self._active[key] = current - 1

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        """Занимает слот на время блока.

        Бросает FloodLimitExceededError, если свободных слотов нет. Освобождение
        в finally: без него сбой внутри блока навсегда занял бы слот, и
        пользователь получил бы вечное «подожди предыдущего».
        """
        if not self.try_acquire(key):
            raise FloodLimitExceededError(key)
        try:
            yield
        finally:
            self.release(key)
