"""Ошибки провайдеров, разделённые по виновнику.

Разделение не косметическое: от него зависит, повторять ли вызов и размыкать
ли предохранитель. Провайдер лёг — повторяем и в конце концов перестаём его
дёргать. Мы прислали ерунду — повторять бессмысленно, а предохранитель трогать
нечестно: провайдер-то жив.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Базовая ошибка обращения к провайдеру."""


class ProviderUnavailableError(ProviderError):
    """Проблема на стороне провайдера: 5xx, 429, обрыв связи, таймаут."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ProviderTimeoutError(ProviderUnavailableError):
    """Провайдер не ответил за отведённое время.

    Отдельный класс, потому что решение о повторе для таймаута особое: он не
    означает, что запрос не выполнился на той стороне. Для картинок повтор
    рискует оплатить одну и ту же работу дважды.
    """


class ProviderRequestError(ProviderError):
    """Провайдер отказал по существу: 4xx. Виноват запрос, а не провайдер."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class ProviderResponseError(ProviderError):
    """Ответ пришёл, но в нём нет того, за чем обращались."""
