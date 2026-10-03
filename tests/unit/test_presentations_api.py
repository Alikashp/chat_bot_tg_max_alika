"""Адаптер Fibonacci API: опрос, таймауты и повторы строго по docs/API.md §8.

Сеть подменена respx, время — управляемыми часами: пять минут ожидания
проходят мгновенно, а каждая пауза записывается, и по ней видно, что повтор
был ровно через 2, 4 и 8 секунд.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from app.adapters.presentations.fibonacci import (
    DECKS_PER_MINUTE,
    POLL_SECONDS,
    FibonacciPresentations,
)
from app.ports.presentations import (
    DeckRequest,
    PresentationBusyError,
    PresentationError,
    PresentationTimeoutError,
)

API = "https://fibonacci-api.test"
KEY = "fib_test"
DECK = "dk_01J9Z6"


class Clock:
    """Часы, которые идут только тогда, когда адаптер спит."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def api(clock: Clock) -> FibonacciPresentations:
    keys = iter(f"key-{n}" for n in range(100))
    return FibonacciPresentations(
        httpx.AsyncClient(),
        base_url=API,
        api_key=KEY,
        max_concurrent=5,
        clock=clock,
        sleep=clock.sleep,
        new_key=lambda: next(keys),
    )


def deck(
    status: str, *, pdf: bool = True, error: str | None = None
) -> dict[str, object]:
    files = None
    if status == "done":
        files = {
            "pptx": f"{API}/v1/decks/{DECK}/files/pptx",
            "pdf": f"{API}/v1/decks/{DECK}/files/pdf" if pdf else None,
            "expires_at": "2026-10-01T14:08:21Z",
        }
    return {
        "id": DECK,
        "status": status,
        "files": files,
        "warnings": [],
        "error": {"code": error, "message": "текст для человека"} if error else None,
    }


def mock_files(pdf_status: int = 200) -> None:
    respx.get(f"{API}/v1/decks/{DECK}/files/pptx").mock(
        return_value=httpx.Response(200, content=b"PPTX")
    )
    respx.get(f"{API}/v1/decks/{DECK}/files/pdf").mock(
        return_value=httpx.Response(
            pdf_status,
            content=b"PDF" if pdf_status == 200 else b"",
            json=None
            if pdf_status == 200
            else {"error": {"code": "FILE_NOT_AVAILABLE"}},
        )
    )


# --- Путь целиком --------------------------------------------------------


def request(topic: str) -> DeckRequest:
    """Колода по одной теме в оформлении «Лазурь», остальное — по умолчанию."""
    return DeckRequest(topic=topic, theme_id="azure_coral")


@respx.mock
async def test_create_poll_and_download(
    api: FibonacciPresentations, clock: Clock
) -> None:
    """Создали → опрашиваем раз в 4 с до done → сразу скачали оба файла."""
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("queued"))
    )
    status = respx.get(f"{API}/v1/decks/{DECK}").mock(
        side_effect=[
            httpx.Response(200, json=deck("processing")),
            httpx.Response(200, json=deck("done")),
        ]
    )
    mock_files()

    built = await api.build(request("Как работает фотосинтез"))

    assert (built.pptx, built.pdf) == (b"PPTX", b"PDF")
    assert status.call_count == 2
    assert clock.slept == [POLL_SECONDS, POLL_SECONDS]
    sent = create.calls.last.request
    assert json.loads(sent.content) == {
        "input": {"topic": "Как работает фотосинтез"},
        "presentation_type": "doklad",
        "theme_id": "azure_coral",
        "language": "ru",
        "slides_count": 9,
        "audience": "general",
    }
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert sent.headers["Idempotency-Key"] == "key-0"


@respx.mock
async def test_every_screen_parameter_reaches_the_api(
    api: FibonacciPresentations,
) -> None:
    """Язык, число слайдов, аудитория — полями запроса под именами из §3.1."""
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done"))
    )
    mock_files()

    await api.build(
        DeckRequest(
            topic="Итоги квартала",
            theme_id="fresh_green",
            language="kk",
            slides=12,
            audience="investors",
        )
    )

    sent = json.loads(create.calls.last.request.content)
    assert (sent["language"], sent["slides_count"], sent["audience"]) == (
        "kk",
        12,
        "investors",
    )
    assert sent["theme_id"] == "fresh_green"


@respx.mock
async def test_the_report_goes_as_material(api: FibonacciPresentations) -> None:
    """Презентация по докладу: его текст — полем input.text (§3.1)."""
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done"))
    )
    mock_files()

    await api.build(
        DeckRequest(
            topic="Фотосинтез", theme_id="azure_coral", material="Текст доклада"
        )
    )

    sent = json.loads(create.calls.last.request.content)
    assert sent["input"] == {"topic": "Фотосинтез", "text": "Текст доклада"}


@respx.mock
async def test_timeouts_follow_section_8(api: FibonacciPresentations) -> None:
    """Подключение 10 с; создание и файлы — 60 с; статус — 10 с."""
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done"))
    )
    mock_files()

    await api.build(request("Тема доклада"))

    post_timeout = create.calls.last.request.extensions["timeout"]
    assert post_timeout == {"connect": 10.0, "read": 60.0, "write": 60.0, "pool": 60.0}
    pptx = respx.calls[1].request.extensions["timeout"]
    assert pptx["read"] == 60.0 and pptx["connect"] == 10.0


@respx.mock
async def test_status_is_read_with_ten_seconds(api: FibonacciPresentations) -> None:
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("queued"))
    )
    status = respx.get(f"{API}/v1/decks/{DECK}").mock(
        return_value=httpx.Response(200, json=deck("done"))
    )
    mock_files()

    await api.build(request("Тема доклада"))

    timeout = status.calls.last.request.extensions["timeout"]
    assert (timeout["connect"], timeout["read"]) == (10.0, 10.0)


@respx.mock
async def test_files_are_fetched_from_our_api_not_from_the_links(
    api: FibonacciPresentations,
) -> None:
    """Ключ уходит в заголовке — только на наш адрес, не по ссылке из ответа."""
    elsewhere = deck("done")
    elsewhere["files"] = {"pptx": "https://evil.test/x", "pdf": "https://evil.test/y"}
    respx.post(f"{API}/v1/decks").mock(return_value=httpx.Response(202, json=elsewhere))
    mock_files()

    await api.build(request("Тема доклада"))

    assert all(call.request.url.host == "fibonacci-api.test" for call in respx.calls)


# --- PDF необязателен ----------------------------------------------------


@respx.mock
async def test_a_missing_pdf_is_not_requested(api: FibonacciPresentations) -> None:
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done", pdf=False))
    )
    mock_files()

    built = await api.build(request("Тема доклада"))

    assert built.pdf is None
    assert not any(c.request.url.path.endswith("/pdf") for c in respx.calls)


@respx.mock
async def test_a_pdf_that_did_not_build_leaves_the_pptx(
    api: FibonacciPresentations,
) -> None:
    """404 FILE_NOT_AVAILABLE: PDF не получился, PPTX есть (§3.3)."""
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done"))
    )
    mock_files(pdf_status=404)

    built = await api.build(request("Тема доклада"))

    assert (built.pptx, built.pdf) == (b"PPTX", None)


# --- Повторы по §8 -------------------------------------------------------


@respx.mock
async def test_a_failing_create_is_retried_with_the_same_key(
    api: FibonacciPresentations, clock: Clock
) -> None:
    """500 и 503 — повтор через 2, 4, 8 с, и ключ идемпотентности тот же."""
    create = respx.post(f"{API}/v1/decks").mock(
        side_effect=[
            httpx.Response(500, json={"error": {"code": "INTERNAL"}}),
            httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE"}}),
            httpx.ConnectError("обрыв"),
            httpx.Response(200, json=deck("done")),
        ]
    )
    mock_files()

    await api.build(request("Тема доклада"))

    assert clock.slept[:3] == [2.0, 4.0, 8.0]
    keys = {call.request.headers["Idempotency-Key"] for call in create.calls}
    assert keys == {"key-0"}
    assert create.call_count == 4


@respx.mock
async def test_three_retries_and_then_a_failure(api: FibonacciPresentations) -> None:
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(500, json={"error": {"code": "INTERNAL"}})
    )

    with pytest.raises(PresentationError) as raised:
        await api.build(request("Тема доклада"))

    assert create.call_count == 4
    assert raised.value.code == "INTERNAL"


@pytest.mark.parametrize("status", [400, 401, 404, 413, 502])
@respx.mock
async def test_other_errors_are_not_retried(
    api: FibonacciPresentations, status: int
) -> None:
    """4xx кроме 429 не повторяем; 502 в §8 не назван — тоже не повторяем."""
    create = respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(status, json={"error": {"code": "BAD_REQUEST"}})
    )

    with pytest.raises(PresentationError) as raised:
        await api.build(request("Тема доклада"))

    assert create.call_count == 1
    assert not isinstance(raised.value, PresentationBusyError)


@respx.mock
async def test_429_waits_retry_after(api: FibonacciPresentations, clock: Clock) -> None:
    create = respx.post(f"{API}/v1/decks").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"error": {"code": "RATE_LIMITED"}},
            ),
            httpx.Response(202, json=deck("done")),
        ]
    )
    mock_files()

    await api.build(request("Тема доклада"))

    assert create.call_count == 2
    assert clock.slept[0] == 7.0


@respx.mock
async def test_a_429_longer_than_the_wait_is_busy(api: FibonacciPresentations) -> None:
    """Суточный лимит ключа ждать до полуночи нельзя: «много запросов»."""
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "36000"},
            json={"error": {"code": "DAILY_LIMIT_EXCEEDED"}},
        )
    )

    with pytest.raises(PresentationBusyError) as raised:
        await api.build(request("Тема доклада"))

    assert raised.value.reached_api is True
    assert raised.value.code == "DAILY_LIMIT_EXCEEDED"


# --- Ожидание колоды ----------------------------------------------------


@respx.mock
async def test_a_failed_deck_names_its_code(api: FibonacciPresentations) -> None:
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("queued"))
    )
    respx.get(f"{API}/v1/decks/{DECK}").mock(
        return_value=httpx.Response(200, json=deck("failed", error="OUTLINE_FAILED"))
    )

    with pytest.raises(PresentationError) as raised:
        await api.build(request("Тема доклада"))

    assert raised.value.code == "OUTLINE_FAILED"
    # Сообщение провайдера в ошибку не попадает: человеку — свои тексты.
    assert "текст для человека" not in str(raised.value)


@respx.mock
async def test_five_minutes_is_the_limit(
    api: FibonacciPresentations, clock: Clock
) -> None:
    """Колода не готова за 5 минут с создания — неудача (§8)."""
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("queued"))
    )
    status = respx.get(f"{API}/v1/decks/{DECK}").mock(
        return_value=httpx.Response(200, json=deck("processing"))
    )
    started = clock.now

    with pytest.raises(PresentationTimeoutError):
        await api.build(request("Тема доклада"))

    assert clock.now - started <= 300.0
    assert status.call_count == 74  # раз в 4 с, без последнего за чертой
    assert all(pause <= POLL_SECONDS for pause in clock.slept)


# --- Пределы ключа (К8) --------------------------------------------------


@respx.mock
async def test_over_the_concurrency_limit_nothing_is_sent(clock: Clock) -> None:
    """Сверх предела одновременных сборок — отказ до всякого запроса.

    Первая сборка задержана на опросе настоящим событием: без этого она
    дошла бы до конца раньше, чем начнётся вторая, и тест прошёл бы и без
    предела.
    """
    api = FibonacciPresentations(
        httpx.AsyncClient(),
        base_url=API,
        api_key=KEY,
        max_concurrent=1,
        clock=clock,
        sleep=clock.sleep,
    )
    release = asyncio.Event()
    polling = asyncio.Event()

    async def held_status(_request: httpx.Request) -> httpx.Response:
        polling.set()
        await release.wait()
        return httpx.Response(200, json=deck("done"))

    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("queued"))
    )
    respx.get(f"{API}/v1/decks/{DECK}").mock(side_effect=held_status)
    mock_files()

    first = asyncio.create_task(api.build(request("Первая тема")))
    await asyncio.wait_for(polling.wait(), timeout=5)
    posts_before = respx.routes[0].call_count

    # Срок на случай поломки: без предела вторая сборка встала бы на том же
    # задержанном опросе, и тест не упал бы, а повис.
    with pytest.raises(PresentationBusyError) as raised:
        await asyncio.wait_for(api.build(request("Вторая тема")), timeout=5)

    assert raised.value.reached_api is False
    assert respx.routes[0].call_count == posts_before
    release.set()
    assert (await first).pptx == b"PPTX"


@respx.mock
async def test_no_more_than_ten_decks_a_minute(
    api: FibonacciPresentations, clock: Clock
) -> None:
    """§7: десять колод в минуту — одиннадцатая ждёт следующей минуты."""
    respx.post(f"{API}/v1/decks").mock(
        return_value=httpx.Response(202, json=deck("done"))
    )
    mock_files()

    for _ in range(DECKS_PER_MINUTE):
        await api.build(request("Тема доклада"))
    posts = respx.routes[0].call_count

    with pytest.raises(PresentationBusyError) as raised:
        await api.build(request("Тема доклада"))
    assert raised.value.reached_api is False
    assert respx.routes[0].call_count == posts

    clock.now += 60
    await api.build(request("Тема доклада"))


async def test_requests_stay_under_120_a_minute(clock: Clock) -> None:
    """§7: за любую минуту запросов не больше 120 — ограничитель их придерживает."""
    sent: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        return httpx.Response(200, json={"themes": []})

    api = FibonacciPresentations(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url=API,
        api_key=KEY,
        max_concurrent=5,
        clock=clock,
        sleep=clock.sleep,
    )
    for _ in range(300):
        with pytest.raises(PresentationError):
            await api.themes()

    start = sent[0]
    window = [moment for moment in sent if moment - start < 60]
    assert len(window) <= 120


# --- Оформления ----------------------------------------------------------


@respx.mock
async def test_themes_come_from_the_api_and_are_cached_for_a_day(
    api: FibonacciPresentations, clock: Clock
) -> None:
    route = respx.get(f"{API}/v1/themes").mock(
        return_value=httpx.Response(
            200,
            json={
                "themes": [
                    {
                        "id": "graphite_light",
                        "name": {"ru": "Графит светлая", "en": "Graphite"},
                    },
                    {
                        "id": "fresh_green",
                        "name": {"en": "Fresh green"},
                        "new_field": 1,
                    },
                    {"id": "Плохой id!", "name": {"ru": "Сломанная"}},
                ]
            },
        )
    )

    themes = await api.themes()
    await api.themes()

    assert [(t.id, t.name) for t in themes] == [
        ("graphite_light", "Графит светлая"),
        ("fresh_green", "Fresh green"),
    ]
    assert route.call_count == 1

    clock.now += 24 * 60 * 60
    await api.themes()
    assert route.call_count == 2
