"""Тесты обработчика вебхука.

Главное, что здесь проверяется, — §3.4.1: обработчик валидирует запрос,
отдаёт работу в фон и отвечает 200 OK, не дожидаясь результата.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.infra.server import TELEGRAM_SECRET_HEADER, create_app
from app.infra.tasks import BackgroundTasks

SECRET = "test-webhook-secret-value"
PATH = "/webhook/telegram"


class Harness:
    """Приложение под тестом вместе с записанными обновлениями."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.tasks = BackgroundTasks()
        self.release = asyncio.Event()
        self.release.set()

    async def handle(self, update: dict[str, Any]) -> None:
        await self.release.wait()
        self.received.append(update)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.tasks.accepting else "shutting_down",
            "in_flight": self.tasks.running,
        }

    def app(self) -> web.Application:
        return create_app(
            telegram_secret=SECRET,
            telegram_webhook_path=PATH,
            handle_update=self.handle,
            tasks=self.tasks,
            health=self.health,
        )


@pytest.fixture
async def harness() -> Harness:
    return Harness()


#: TestClient параметризован типами запроса и приложения.
WebClient = TestClient[web.Request, web.Application]


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[WebClient]:
    client: WebClient = TestClient(TestServer(harness.app()))
    await client.start_server()
    yield client
    await client.close()


async def test_valid_update_is_accepted(client: WebClient, harness: Harness) -> None:
    response = await client.post(
        PATH, json={"update_id": 1}, headers={TELEGRAM_SECRET_HEADER: SECRET}
    )

    assert response.status == 200
    await harness.tasks.drain(timeout=1.0)
    assert harness.received == [{"update_id": 1}]


async def test_request_without_secret_is_rejected(
    client: WebClient, harness: Harness
) -> None:
    response = await client.post(PATH, json={"update_id": 1})

    assert response.status == 403
    assert harness.received == []


async def test_request_with_wrong_secret_is_rejected(
    client: WebClient, harness: Harness
) -> None:
    response = await client.post(
        PATH, json={"update_id": 1}, headers={TELEGRAM_SECRET_HEADER: "не тот"}
    )

    assert response.status == 403
    assert harness.received == []


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
    response = await client.post(
        PATH, json=[1, 2, 3], headers={TELEGRAM_SECRET_HEADER: SECRET}
    )

    assert response.status == 400


async def test_response_does_not_wait_for_the_work(
    client: WebClient, harness: Harness
) -> None:
    """Главный тест фазы: 200 OK приходит до того, как работа сделана.

    Обработчик держится на невзведённом событии; если бы ответ ждал его,
    запрос завис бы, а не вернулся мгновенно.
    """
    harness.release.clear()

    response = await asyncio.wait_for(
        client.post(
            PATH, json={"update_id": 7}, headers={TELEGRAM_SECRET_HEADER: SECRET}
        ),
        timeout=1.0,
    )

    assert response.status == 200
    assert harness.received == []

    harness.release.set()
    await harness.tasks.drain(timeout=1.0)
    assert harness.received == [{"update_id": 7}]


async def test_health_reports_ok_and_in_flight(
    client: WebClient, harness: Harness
) -> None:
    harness.release.clear()
    await client.post(
        PATH, json={"update_id": 1}, headers={TELEGRAM_SECRET_HEADER: SECRET}
    )

    response = await client.get("/health")

    assert response.status == 200
    assert await response.json() == {"status": "ok", "in_flight": 1}

    harness.release.set()
    await harness.tasks.drain(timeout=1.0)


async def test_health_is_unhealthy_while_shutting_down(
    client: WebClient, harness: Harness
) -> None:
    """Во время остановки Railway должен уводить трафик с этого экземпляра."""
    harness.tasks.stop_accepting()

    response = await client.get("/health")

    assert response.status == 503
    assert (await response.json())["status"] == "shutting_down"


async def test_updates_are_refused_while_shutting_down(
    client: WebClient, harness: Harness
) -> None:
    """503 честнее, чем 200: мессенджер повторит обновление позже."""
    harness.tasks.stop_accepting()

    response = await client.post(
        PATH, json={"update_id": 1}, headers={TELEGRAM_SECRET_HEADER: SECRET}
    )

    assert response.status == 503
    assert harness.received == []
