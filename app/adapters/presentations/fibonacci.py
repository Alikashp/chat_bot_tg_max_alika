"""Клиент Fibonacci AI REST API v1 (docs/API.md).

Колода собирается асинхронно: создать → опрашивать статус → скачать файлы.
Всё, что касается времени и повторов, здесь сделано строго по разделу 8
документа, а не по вкусу:

* подключение — 10 с; создание колоды и скачивание файла — 60 с на чтение;
  запрос статуса и списка оформлений — 10 с;
* статус опрашивается раз в 4 с (документ просит 3–5 и не чаще раза в 2);
* колоду ждём не дольше 5 минут с момента создания, потом это неудача;
* сбой сети, 500 и 503 — повтор с паузами 2, 4, 8 с, не больше трёх раз;
  создание колоды повторяется с тем же ``Idempotency-Key``, и вторая колода
  от повтора не появляется;
* 429 — ждём ``Retry-After`` и повторяем; прочие 4xx не повторяем вовсе.

Файлы скачиваются сразу, как колода готова: у провайдера они живут час.
Скачиваются по нашему адресу API, а не по ссылкам из ответа: ссылки ведут
туда же, но ключ уходит в заголовке, и отдавать его по адресу, пришедшему
снаружи, незачем.

Пределы ключа (120 запросов и 10 колод в минуту, §7) держатся здесь же, до
обращения к провайдеру. Сверх предела сборка не начинается вовсе — ядро
получает ``PresentationBusyError`` и говорит человеку «много запросов».
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.infra.logging import get_logger
from app.infra.ratelimit import TokenBucket
from app.ports.presentations import (
    PRESENTATION_TYPE,
    BuiltPresentation,
    DeckRequest,
    PresentationBusyError,
    PresentationError,
    PresentationTheme,
    PresentationTimeoutError,
)

logger = get_logger(__name__)

#: Таймауты по §8.
CONNECT_SECONDS = 10.0
CREATE_READ_SECONDS = 60.0
STATUS_READ_SECONDS = 10.0
DOWNLOAD_READ_SECONDS = 60.0

#: Как часто спрашиваем статус и сколько ждём колоду (§8).
POLL_SECONDS = 4.0
WAIT_SECONDS = 300.0

#: Паузы между повторами при сбое сети, 500 и 503 (§8): три повтора.
RETRY_PAUSES = (2.0, 4.0, 8.0)

#: Ответы, которые повторяем с паузой (§8). Остальные 5xx в документе не
#: названы, и строго по нему они не повторяются.
_RETRY_STATUSES = frozenset({500, 503})

#: Сколько ждать, если 429 пришёл без Retry-After.
_DEFAULT_RETRY_AFTER = 5.0

#: Пределы ключа по умолчанию (§7). Запросы считаем с запасом: 114 в минуту
#: плюс всплеск в 6 — не больше 120 ни в какую календарную минуту.
REQUESTS_PER_SECOND = 1.9
REQUEST_BURST = 6
DECKS_PER_MINUTE = 10

#: Оформления меняются редко; документ разрешает кэшировать их на сутки.
THEMES_TTL_SECONDS = 24 * 60 * 60

#: Какие идентификаторы оформлений пропускаем в кнопку: короткие и без
#: неожиданных символов. Идентификатор уезжает в данные кнопки, а там у
#: Telegram 64 байта на всё.
_THEME_ID = re.compile(r"^[a-z0-9_-]{1,40}$")

#: Идентификатор колоды подставляется в путь запроса — только безопасный.
_DECK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Сколько оформлений показываем. Список пополняется, а кнопок под одним
#: сообщением должно быть столько, чтобы их можно было прочесть.
MAX_THEMES = 8

_IN_PROGRESS = frozenset({"queued", "processing"})
#: ``done_pdf_pending`` зарезервирован провайдером и обрабатывается как done.
_DONE = frozenset({"done", "done_pdf_pending"})
_FAILED = "failed"


class FibonacciPresentations:
    """Реализация порта Presentations."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        max_concurrent: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        new_key: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("одновременных сборок должно быть хотя бы одна")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._max_concurrent = max_concurrent
        self._running = 0
        self._clock = clock
        self._sleep = sleep
        self._new_key = new_key
        self._requests = TokenBucket(
            rate=REQUESTS_PER_SECOND, burst=REQUEST_BURST, clock=clock, sleep=sleep
        )
        self._decks = _Window(limit=DECKS_PER_MINUTE, period=60.0, clock=clock)
        self._themes: tuple[PresentationTheme, ...] = ()
        self._themes_at = 0.0

    # --- Порт ----------------------------------------------------------

    async def themes(self) -> tuple[PresentationTheme, ...]:
        """Оформления из API, сутки из памяти."""
        if self._themes and self._clock() - self._themes_at < THEMES_TTL_SECONDS:
            return self._themes

        response = await self._request("GET", "/v1/themes", read=STATUS_READ_SECONDS)
        themes = _themes_of(_json(response))
        if not themes:
            raise PresentationError("NO_THEMES")
        self._themes = themes
        self._themes_at = self._clock()
        return themes

    async def build(self, request: DeckRequest) -> BuiltPresentation:
        """Создать, дождаться, скачать. Слот и место в минуте — до запроса."""
        if self._running >= self._max_concurrent:
            raise PresentationBusyError("TOO_MANY_BUILDS")
        if not self._decks.try_take():
            raise PresentationBusyError("TOO_MANY_DECKS")

        self._running += 1
        try:
            return await self._build(request)
        finally:
            self._running -= 1

    # --- Сборка --------------------------------------------------------

    async def _build(self, request: DeckRequest) -> BuiltPresentation:
        deadline = self._clock() + WAIT_SECONDS
        params = _params(request)
        # Файл — multipart: поле params с теми же параметрами в JSON и поле
        # file (§2, §3.1). Без файла — обычный JSON.
        multipart = (
            {
                "data": {"params": json.dumps(params, ensure_ascii=False)},
                "files": {
                    "file": (
                        request.file.filename,
                        request.file.data,
                        request.file.mime_type,
                    )
                },
            }
            if request.file is not None
            else None
        )
        response = await self._request(
            "POST",
            "/v1/decks",
            read=CREATE_READ_SECONDS,
            json=params if multipart is None else None,
            multipart=multipart,
            # Один ключ на колоду — для всех повторов её создания (§3.1, §8).
            headers={"Idempotency-Key": self._new_key()},
            deadline=deadline,
            creating=True,
        )
        deck = _json(response)
        deck_id = _deck_id(deck)

        while (status := deck.get("status")) not in _DONE:
            if status == _FAILED:
                raise PresentationError(_deck_error(deck))
            left = deadline - self._clock()
            if left <= 0:
                raise PresentationTimeoutError("CLIENT_TIMEOUT")
            await self._sleep(min(POLL_SECONDS, left))
            if self._clock() >= deadline:
                raise PresentationTimeoutError("CLIENT_TIMEOUT")
            deck = _json(
                await self._request(
                    "GET",
                    f"/v1/decks/{deck_id}",
                    read=STATUS_READ_SECONDS,
                    deadline=deadline,
                )
            )

        pptx = await self._download(deck_id, "pptx")
        pdf = await self._optional_pdf(deck_id, deck)
        return BuiltPresentation(pptx=pptx, pdf=pdf)

    async def _download(self, deck_id: str, kind: str) -> bytes:
        response = await self._request(
            "GET", f"/v1/decks/{deck_id}/files/{kind}", read=DOWNLOAD_READ_SECONDS
        )
        if not response.content:
            raise PresentationError("EMPTY_FILE")
        return response.content

    async def _optional_pdf(self, deck_id: str, deck: dict[str, Any]) -> bytes | None:
        """PDF, если он собрался. Его отсутствие — не сбой: PPTX полноценный."""
        files = deck.get("files")
        if not isinstance(files, dict) or not files.get("pdf"):
            return None
        try:
            return await self._download(deck_id, "pdf")
        except PresentationError as error:
            logger.warning("presentation_pdf_missing", code=error.code)
            return None

    # --- Запрос с повторами по §8 --------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        read: float,
        json: dict[str, Any] | None = None,
        multipart: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        deadline: float | None = None,
        creating: bool = False,
    ) -> httpx.Response:
        """Один запрос к API с повторами строго по разделу 8."""
        timeout = httpx.Timeout(read, connect=CONNECT_SECONDS)
        attempts = len(RETRY_PAUSES) + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            await self._requests.acquire()
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={**self._headers, **(headers or {})},
                    json=json,
                    timeout=timeout,
                    **(multipart or {}),
                )
            except httpx.TransportError as error:
                if last:
                    raise PresentationError("NETWORK") from error
                await self._pause(RETRY_PAUSES[attempt], attempt, "network")
                continue

            status = response.status_code
            if status < 400:
                return response

            code = _error_code(response)
            if status == 429:
                wait = _retry_after(response)
                too_long = deadline is not None and self._clock() + wait >= deadline
                if last or too_long:
                    # Лимит ключа исчерпан: ни сборки, ни списания, человеку
                    # — «много запросов, попробуй позже».
                    raise PresentationBusyError(code, reached_api=True)
                await self._pause(wait, attempt, code)
                continue

            if status in _RETRY_STATUSES and not last:
                await self._pause(RETRY_PAUSES[attempt], attempt, code)
                continue

            if creating and status == 400:
                logger.warning("presentation_rejected", code=code)
            raise PresentationError(code)

        raise PresentationError("RETRIES_EXHAUSTED")  # pragma: no cover

    async def _pause(self, seconds: float, attempt: int, reason: str) -> None:
        logger.info(
            "presentation_api_retry", attempt=attempt + 1, reason=reason, wait=seconds
        )
        await self._sleep(seconds)


class _Window:
    """Не больше ``limit`` событий за последние ``period`` секунд.

    Скользящее окно строже календарной минуты провайдера: если в любые
    шестьдесят секунд подряд колод не больше десяти, то и в календарную
    минуту их не больше.
    """

    def __init__(
        self, *, limit: int, period: float, clock: Callable[[], float]
    ) -> None:
        self._limit = limit
        self._period = period
        self._clock = clock
        self._times: deque[float] = deque()

    def try_take(self) -> bool:
        now = self._clock()
        while self._times and now - self._times[0] >= self._period:
            self._times.popleft()
        if len(self._times) >= self._limit:
            return False
        self._times.append(now)
        return True


# --- Разбор ответов ------------------------------------------------------


def _params(request: DeckRequest) -> dict[str, Any]:
    """Параметры колоды в терминах API (§3.1).

    Тип называется явно, хотя он и по умолчанию: пока он один, но запрос
    должен говорить, что собирать, а не полагаться на чужие умолчания.
    Текст-материал — полем input.text; с файлом текста не бывает.
    """
    source: dict[str, str] = {"topic": request.topic}
    if request.material and request.file is None:
        source["text"] = request.material
    return {
        "input": source,
        "presentation_type": PRESENTATION_TYPE,
        "theme_id": request.theme_id,
        "language": request.language,
        "slides_count": request.slides,
        "audience": request.audience,
    }


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise PresentationError("BAD_JSON") from error
    if not isinstance(payload, dict):
        raise PresentationError("BAD_JSON")
    return payload


def _deck_id(deck: dict[str, Any]) -> str:
    deck_id = deck.get("id")
    if not isinstance(deck_id, str) or not _DECK_ID.match(deck_id):
        raise PresentationError("BAD_DECK_ID")
    return deck_id


def _deck_error(deck: dict[str, Any]) -> str:
    """Код ошибки колоды. Сообщение провайдера человеку не показываем."""
    error = deck.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else "DECK_FAILED"


def _error_code(response: httpx.Response) -> str:
    """Машинный код из тела ошибки; номер ответа, если кода нет."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP_{response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else f"HTTP_{response.status_code}"


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_RETRY_AFTER


def _themes_of(payload: dict[str, Any]) -> tuple[PresentationTheme, ...]:
    """Оформления из ответа. Незнакомые поля игнорируем (§10)."""
    raw = payload.get("themes")
    if not isinstance(raw, list):
        return ()
    themes: list[PresentationTheme] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        theme_id = item.get("id")
        if not isinstance(theme_id, str) or not _THEME_ID.match(theme_id):
            continue
        themes.append(PresentationTheme(id=theme_id, name=_name_of(item, theme_id)))
    return tuple(themes[:MAX_THEMES])


def _name_of(item: dict[str, Any], fallback: str) -> str:
    names = item.get("name")
    if isinstance(names, dict):
        for language in ("ru", "en"):
            name = names.get(language)
            if isinstance(name, str) and name.strip():
                return name.strip()[:40]
    return fallback
