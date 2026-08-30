"""Провайдеры ИИ: разбор ответов, ошибки и обвязка вокруг вызова.

Сеть подменена на уровне транспорта httpx (respx), поэтому проверяется
настоящий код провайдера целиком — вместе с заголовками, телом запроса и
разбором ответа.

Главное, что здесь доказывается: ошибка провайдера и ошибка нашего запроса
различаются. От этого зависит, повторять ли вызов и размыкать ли цепь, а
значит — сколько денег уйдёт провайдеру в час его неисправности.
"""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest
import respx

from app.adapters.ai.errors import (
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.adapters.ai.images import OpenAIImages
from app.adapters.ai.resilience import ProviderPolicy, ResilientCaller
from app.adapters.ai.text import OpenAICompatibleLLM
from app.core.models import ChatTurn, Photo, Role
from app.infra.retry import RetryPolicy
from app.ports.ai import ContentRefusedError, ImageQuality
from tests.fakes import PNG_BYTES

BASE = "https://api.example.com/v1"
CHAT_URL = f"{BASE}/chat/completions"
IMAGE_URL = f"{BASE}/images/generations"
EDIT_URL = f"{BASE}/images/edits"


def _caller(**overrides: object) -> ResilientCaller:
    """Обвязка без задержек: тест не должен спать по полсекунды."""
    policy = ProviderPolicy(
        rate=1000.0,
        burst=1000,
        concurrency=8,
        retry=RetryPolicy(attempts=3, base_delay=0.001, max_delay=0.002, jitter=0.0),
        **overrides,  # type: ignore[arg-type]
    )
    return ResilientCaller("test", policy)


def _llm(caller: ResilientCaller | None = None) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        httpx.AsyncClient(),
        base_url=BASE,
        api_key="sk-test",
        caller=caller or _caller(),
        system_prompt="Be brief.",
        max_tokens=256,
    )


def _images(caller: ResilientCaller | None = None) -> OpenAIImages:
    return OpenAIImages(
        httpx.AsyncClient(),
        base_url=BASE,
        api_key="sk-test",
        caller=caller or _caller(),
        model="gpt-image-1",
        size="1024x1024",
    )


def _answer(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _drawn() -> dict[str, object]:
    return {"data": [{"b64_json": b64encode(PNG_BYTES).decode()}]}


TURNS = (ChatTurn(Role.USER, "какая столица Франции?"),)


# --- Текст ---------------------------------------------------------------


@respx.mock
async def test_the_answer_is_extracted() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_answer("  Париж.  "))
    )

    assert await _llm().complete(TURNS, model="gpt-5") == "Париж."


@respx.mock
async def test_the_request_carries_the_system_prompt_and_the_key() -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_answer("Париж."))
    )

    await _llm().complete(TURNS, model="gpt-5-mini")

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    payload = __import__("json").loads(request.content)
    assert payload["model"] == "gpt-5-mini"
    assert payload["messages"][0] == {"role": "system", "content": "Be brief."}
    assert payload["messages"][-1]["role"] == "user"


@respx.mock
async def test_an_empty_answer_is_a_failure_not_an_answer() -> None:
    """Пустое сообщение хуже честной ошибки: за ошибку лимит не спишется."""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_answer("   ")))

    with pytest.raises(ProviderResponseError):
        await _llm().complete(TURNS, model="gpt-5")


@respx.mock
async def test_a_broken_answer_shape_is_a_failure() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"choices": []}))

    with pytest.raises(ProviderResponseError):
        await _llm().complete(TURNS, model="gpt-5")


# --- Кто виноват ---------------------------------------------------------


@respx.mock
async def test_a_server_error_is_retried_and_can_succeed() -> None:
    respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_answer("Париж.")),
        ]
    )

    assert await _llm().complete(TURNS, model="gpt-5") == "Париж."


@respx.mock
async def test_too_many_requests_is_treated_as_temporary() -> None:
    """429 — это «слишком часто», а не «неверный запрос»: лечится ожиданием."""
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=_answer("Париж.")),
        ]
    )

    await _llm().complete(TURNS, model="gpt-5")

    assert route.call_count == 2


@respx.mock
async def test_a_rejected_request_is_not_retried() -> None:
    """400 — виноват запрос. Повторять его значит просто ждать дважды."""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(400))

    with pytest.raises(ProviderRequestError):
        await _llm().complete(TURNS, model="gpt-5")

    assert route.call_count == 1


@respx.mock
async def test_the_error_message_never_carries_the_response_body() -> None:
    """Провайдеры любят вернуть эхо запроса, а в нём — сообщение человека."""
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, json={"error": "какая столица Франции?"})
    )

    with pytest.raises(ProviderRequestError) as failure:
        await _llm().complete(TURNS, model="gpt-5")

    assert "столица" not in str(failure.value)


@respx.mock
async def test_a_broken_connection_is_the_providers_fault() -> None:
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("нет связи"))

    with pytest.raises(ProviderUnavailableError):
        await _llm().complete(TURNS, model="gpt-5")


@respx.mock
async def test_a_timeout_is_reported_separately() -> None:
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("не дождались"))

    with pytest.raises(ProviderTimeoutError):
        await _llm().complete(TURNS, model="gpt-5")


@respx.mock
async def test_the_breaker_stops_hammering_a_dead_provider() -> None:
    """Лежащему провайдеру не помогает, когда в него продолжают стучать."""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(503))
    caller = _caller(failure_threshold=1, recovery_seconds=60.0)
    llm = _llm(caller)

    with pytest.raises(ProviderUnavailableError):
        await llm.complete(TURNS, model="gpt-5")
    calls_after_first = route.call_count

    with pytest.raises(Exception):  # noqa: B017 — цепь разомкнута, класс иной
        await llm.complete(TURNS, model="gpt-5")

    assert route.call_count == calls_after_first, "звонок ушёл в разомкнутую цепь"


# --- Картинки ------------------------------------------------------------


@respx.mock
async def test_a_drawn_image_is_decoded() -> None:
    respx.post(IMAGE_URL).mock(return_value=httpx.Response(200, json=_drawn()))

    photo = await _images().generate("кот", quality=ImageQuality.LOW)

    assert photo.data == PNG_BYTES


@respx.mock
async def test_the_quality_comes_from_the_tariff() -> None:
    route = respx.post(IMAGE_URL).mock(return_value=httpx.Response(200, json=_drawn()))

    await _images().generate("кот", quality=ImageQuality.MEDIUM)

    payload = __import__("json").loads(route.calls.last.request.content)
    assert payload["quality"] == "medium"


@respx.mock
async def test_editing_sends_the_source_photo_as_a_file() -> None:
    """Пресеты требуют именно редактирования — значит multipart, не JSON."""
    route = respx.post(EDIT_URL).mock(return_value=httpx.Response(200, json=_drawn()))

    await _images().edit(
        Photo(data=PNG_BYTES, mime_type="image/png", filename="in.png"),
        "make it lego",
        quality=ImageQuality.LOW,
    )

    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"make it lego" in request.content
    assert PNG_BYTES in request.content


@respx.mock
async def test_a_link_instead_of_bytes_is_refused() -> None:
    """Ссылка — это второй поход в сеть там, где человек уже ждёт."""
    respx.post(IMAGE_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"url": "https://x/y.png"}]})
    )

    with pytest.raises(ProviderResponseError):
        await _images().generate("кот", quality=ImageQuality.LOW)


@respx.mock
async def test_a_broken_base64_is_refused() -> None:
    respx.post(IMAGE_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"b64_json": "не base64!"}]})
    )

    with pytest.raises(ProviderResponseError):
        await _images().generate("кот", quality=ImageQuality.LOW)


@respx.mock
async def test_an_image_timeout_is_not_retried() -> None:
    """Таймаут не значит, что на той стороне не нарисовали. Повтор — двойная цена."""
    route = respx.post(IMAGE_URL).mock(side_effect=httpx.ReadTimeout("долго"))
    images = _images(_caller(retry_on_timeout=False))

    with pytest.raises(ProviderTimeoutError):
        await images.generate("кот", quality=ImageQuality.LOW)

    assert route.call_count == 1


@respx.mock
async def test_an_image_server_error_is_still_retried() -> None:
    """503 — провайдер до работы даже не дошёл, повторить безопасно."""
    route = respx.post(IMAGE_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=_drawn())]
    )
    images = _images(_caller(retry_on_timeout=False))

    await images.generate("кот", quality=ImageQuality.LOW)

    assert route.call_count == 2


# --- Отказ по содержанию -------------------------------------------------


@respx.mock
async def test_a_moderation_refusal_is_not_a_failure() -> None:
    """Провайдер жив и ответил по существу: повторять и рвать цепь нечего."""
    route = respx.post(IMAGE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "moderation_blocked",
                    "message": "Your request was rejected by our safety system",
                }
            },
        )
    )

    with pytest.raises(ContentRefusedError):
        await _images().generate("что-нибудь запрещённое", quality=ImageQuality.LOW)

    assert route.call_count == 1


@respx.mock
async def test_a_content_policy_refusal_is_recognised_too() -> None:
    """Один и тот же отказ разные API называют по-разному."""
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "content_policy_violation"}}
        )
    )

    with pytest.raises(ContentRefusedError):
        await _llm().complete(TURNS, model="gpt-5")


@respx.mock
async def test_the_refusal_never_carries_the_providers_message() -> None:
    """В сообщении провайдера бывает эхо запроса — ему не место в логах."""
    respx.post(IMAGE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "moderation_blocked",
                    "message": "rejected prompt: голая женщина",
                }
            },
        )
    )

    with pytest.raises(ContentRefusedError) as refusal:
        await _images().generate("голая женщина", quality=ImageQuality.LOW)

    assert "женщина" not in str(refusal.value)


@respx.mock
async def test_an_ordinary_rejection_keeps_its_machine_code() -> None:
    """Без кода отладка отказа сводится к гаданию по номеру статуса."""
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(404, json={"error": {"code": "model_not_found"}})
    )

    with pytest.raises(ProviderRequestError) as failure:
        await _llm().complete(TURNS, model="нет-такой-модели")

    assert "model_not_found" in str(failure.value)
