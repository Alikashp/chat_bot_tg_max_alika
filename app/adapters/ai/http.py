"""Общая работа с HTTP для провайдеров ИИ.

Отдельный слой нужен ради одного: превратить всё разнообразие способов, какими
может не удаться HTTP-запрос, в два понятных случая — «виноват провайдер» и
«виноват запрос». От этого зависит, повторять ли вызов и размыкать ли цепь.

Клиент один на приложение: httpx держит пул соединений, и создавать клиента на
каждый запрос значит на каждом сообщении заново устанавливать TLS.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.ai.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.ports.ai import ContentRefusedError

#: Коды, которые считаем временной неисправностью провайдера.
#: 429 сюда входит: это «слишком часто», а не «неверный запрос», и лечится
#: ожиданием — ровно тем, что делают повторы с разбросом задержки.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Коды, которыми провайдеры сообщают «этот запрос я обслуживать не буду».
#: Разные API называют это по-разному, поэтому набор, а не одно значение.
_REFUSAL_CODES = frozenset(
    {
        "moderation_blocked",
        "content_policy_violation",
        "content_filter",
        "invalid_prompt",
        "safety_violation",
        "sanitizer_error",
    }
)


def create_client(timeout: float) -> httpx.AsyncClient:
    """Создаёт HTTP-клиент с явным таймаутом (§3.4.6).

    Без явного значения httpx ждёт вечно, и один зависший вызов держал бы
    воркер до самой остановки сервиса.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
    )


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Делает запрос и возвращает разобранный JSON.

    Все исключения httpx приводятся к нашим: выше по стеку никто не должен
    знать, какой библиотекой мы ходим в сеть.
    """
    try:
        response = await client.request(
            method, url, headers=headers, json=json, files=files, data=data
        )
    except httpx.TimeoutException as error:
        raise ProviderTimeoutError(
            f"провайдер не ответил вовремя: {error!r}"
        ) from error
    except httpx.HTTPError as error:
        raise ProviderUnavailableError(f"сбой связи: {error!r}") from error

    _raise_for_status(response)

    try:
        payload = response.json()
    except ValueError as error:
        raise ProviderResponseError("ответ провайдера — не JSON") from error

    if not isinstance(payload, dict):
        raise ProviderResponseError("ответ провайдера не является объектом")
    return payload


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    # Текст ответа в сообщение не кладём: провайдеры любят вернуть в теле
    # эхо запроса, а в нём — сообщение пользователя, которому не место
    # ни в логах, ни в трейсбеке (§3.5).
    if status in _RETRYABLE_STATUSES or status >= 500:
        raise ProviderUnavailableError(f"провайдер ответил {status}", status=status)

    code = _error_code(response)
    if code in _REFUSAL_CODES:
        raise ContentRefusedError(code)
    # Код ошибки — короткое машинное слово вроде model_not_found, а не текст
    # запроса. Без него отладка отказа сводится к гаданию по номеру статуса.
    detail = f"{status} ({code})" if code else str(status)
    raise ProviderRequestError(f"провайдер отклонил запрос: {detail}", status=status)


def _error_code(response: httpx.Response) -> str | None:
    """Машинный код ошибки из тела ответа.

    Берём только code и type — короткие опознавательные строки. Само
    сообщение провайдера не трогаем: в нём бывает эхо запроса (§3.5).
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value
    return None
