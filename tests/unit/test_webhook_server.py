"""Тесты HTTP-обработчика вебхука.

Проверяется §3.4.1: обработчик валидирует запрос, отдаёт обновление приёмнику
и отвечает кодом, соответствующим исходу, — не дожидаясь самой работы.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.infra.server import TELEGRAM_SECRET_HEADER, Outcome, create_app

SECRET = "test-webhook-secret-value"
PATH = "/webhook/telegram"
WebClient = TestClient[web.Request, web.Application]


@dataclass
class Recorder:
    """Приёмник обновлений с заранее заданным исходом."""

    outcome: Outcome = Outcome.ACCEPTED
    received: list[dict[str, Any]] = field(default_factory=list)
    health_payload: dict[str, Any] = field(default_factory=lambda: {"status": "ok"})

    def submit(self, update: dict[str, Any]) -> Outcome:
        self.received.append(update)
        return self.outcome

    def health(self) -> dict[str, Any]:
        return self.health_payload


@pytest.fixture
async def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
async def client(recorder: Recorder) -> AsyncIterator[WebClient]:
    app = create_app(
        telegram_secret=SECRET,
        telegram_webhook_path=PATH,
        submit=recorder.submit,
        health=recorder.health,
    )
    test_client: WebClient = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def _post(client: WebClient, payload: Any, *, secret: str | None = SECRET) -> int:
    headers = {} if secret is None else {TELEGRAM_SECRET_HEADER: secret}
    response = await client.post(PATH, json=payload, headers=headers)
    return response.status


async def test_valid_update_is_accepted(client: WebClient, recorder: Recorder) -> None:
    assert await _post(client, {"update_id": 1}) == 200
    assert recorder.received == [{"update_id": 1}]


async def test_request_without_secret_is_rejected(
    client: WebClient, recorder: Recorder
) -> None:
    assert await _post(client, {"update_id": 1}, secret=None) == 403
    assert recorder.received == []


async def test_request_with_wrong_secret_is_rejected(
    client: WebClient, recorder: Recorder
) -> None:
    """Кириллица в заголовке не должна превращать отказ в 500."""
    assert await _post(client, {"update_id": 1}, secret="не тот") == 403
    assert recorder.received == []


async def test_malformed_json_is_rejected(client: WebClient) -> None:
    response = await client.post(
        PATH,
        data="не json".encode(),
        headers={
            TELEGRAM_SECRET_HEADER: SECRET,
            "Content-Type": "application/json",
        },
    )

    assert response.status == 400


async def test_non_object_payload_is_rejected(client: WebClient) -> None:
    assert await _post(client, [1, 2, 3]) == 400


async def test_duplicate_gets_ok(client: WebClient, recorder: Recorder) -> None:
    """Повтору отвечаем 200: работа по нему уже сделана или делается."""
    recorder.outcome = Outcome.DUPLICATE

    assert await _post(client, {"update_id": 1}) == 200


async def test_overload_gets_service_unavailable(
    client: WebClient, recorder: Recorder
) -> None:
    """503 честнее, чем «200 и молча выбросить»: мессенджер повторит."""
    recorder.outcome = Outcome.OVERLOADED

    assert await _post(client, {"update_id": 1}) == 503


async def test_shutdown_gets_service_unavailable(
    client: WebClient, recorder: Recorder
) -> None:
    recorder.outcome = Outcome.STOPPING

    assert await _post(client, {"update_id": 1}) == 503


async def test_update_without_id_gets_bad_request(
    client: WebClient, recorder: Recorder
) -> None:
    recorder.outcome = Outcome.MALFORMED

    assert await _post(client, {"нет": "id"}) == 400


async def test_response_does_not_wait_for_the_work(client: WebClient) -> None:
    """Ответ приходит мгновенно: сама работа делается вне HTTP-запроса."""
    response = await asyncio.wait_for(
        client.post(
            PATH, json={"update_id": 7}, headers={TELEGRAM_SECRET_HEADER: SECRET}
        ),
        timeout=1.0,
    )

    assert response.status == 200


async def test_health_reports_the_payload(
    client: WebClient, recorder: Recorder
) -> None:
    recorder.health_payload = {"status": "ok", "dedup_keys": 3}

    response = await client.get("/health")

    assert response.status == 200
    assert await response.json() == {"status": "ok", "dedup_keys": 3}


async def test_health_is_unhealthy_while_shutting_down(
    client: WebClient, recorder: Recorder
) -> None:
    """Во время остановки Railway должен уводить трафик с этого экземпляра."""
    recorder.health_payload = {"status": "shutting_down"}

    response = await client.get("/health")

    assert response.status == 503
