"""HTTP-сервер: приём вебхуков и health-check.

Ключевое требование §3.4.1: обработчик вебхука только валидирует запрос,
ставит обновление в очередь и отвечает. Никакой работы с ИИ внутри
HTTP-запроса — иначе мессенджер отвалится по таймауту и начнёт ретраить,
а картинка рисуется 15 секунд.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from http import HTTPStatus
from secrets import compare_digest
from typing import Any

from aiohttp import web

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: Заголовок, которым Telegram подписывает каждый запрос вебхука.
#: noqa ниже — ruff видит слово "secret" в имени и считает это захардкоженным
#: паролем; здесь это имя HTTP-заголовка, а не значение секрета.
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105

#: Ограничение на размер тела запроса вебхука. Обновление мессенджера — это
#: небольшой JSON; всё, что заметно больше, к нам отношения не имеет.
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


class Outcome(StrEnum):
    """Что произошло с обновлением на входе."""

    #: Принято в очередь.
    ACCEPTED = "accepted"
    #: Уже видели — повторная доставка.
    DUPLICATE = "duplicate"
    #: Очередь переполнена.
    OVERLOADED = "overloaded"
    #: Сервис останавливается.
    STOPPING = "stopping"
    #: Обновление не похоже на настоящее.
    MALFORMED = "malformed"


#: Функция, которая забирает сырое обновление и говорит, что с ним стало.
UpdateSubmitter = Callable[[dict[str, Any]], Outcome]

#: Функция, возвращающая данные для /health.
HealthProvider = Callable[[], dict[str, Any]]


def _is_authorized(request: web.Request, header: str, secret: str) -> bool:
    """Сверяет секрет вебхука.

    Сравнение через compare_digest, а не ``==``: обычное сравнение строк
    выходит на первом различающемся байте и по времени ответа позволяет
    подобрать секрет.

    Сравниваем именно байты: строковый compare_digest бросает TypeError на
    не-ASCII, и чужой запрос с кириллицей в заголовке превращался бы из
    отказа в 500 с трейсбеком в логах.
    """
    incoming = request.headers.get(header)
    if incoming is None:
        return False
    return compare_digest(incoming.encode("utf-8"), secret.encode("utf-8"))


def create_app(
    *,
    telegram_secret: str,
    telegram_webhook_path: str,
    submit: UpdateSubmitter,
    health: HealthProvider,
) -> web.Application:
    """Собирает aiohttp-приложение."""
    app = web.Application(client_max_size=MAX_WEBHOOK_BODY_BYTES)

    async def telegram_webhook(request: web.Request) -> web.Response:
        if not _is_authorized(request, TELEGRAM_SECRET_HEADER, telegram_secret):
            # Не подсказываем, что именно не так: это чужой запрос.
            logger.warning("webhook_rejected", messenger="telegram")
            return web.Response(status=HTTPStatus.FORBIDDEN)

        try:
            update = await request.json()
        except ValueError:
            logger.warning("webhook_bad_json", messenger="telegram")
            return web.Response(status=HTTPStatus.BAD_REQUEST)

        if not isinstance(update, dict):
            logger.warning("webhook_bad_payload", messenger="telegram")
            return web.Response(status=HTTPStatus.BAD_REQUEST)

        outcome = submit(update)
        return web.Response(status=_status_for(outcome))

    async def health_check(_request: web.Request) -> web.Response:
        payload = health()
        status = (
            HTTPStatus.OK
            if payload.get("status") == "ok"
            else HTTPStatus.SERVICE_UNAVAILABLE
        )
        return web.json_response(payload, status=status)

    app.router.add_post(telegram_webhook_path, telegram_webhook)
    app.router.add_get("/health", health_check)
    return app


def _status_for(outcome: Outcome) -> HTTPStatus:
    """Переводит исход в HTTP-код.

    Повтору отвечаем 200: работа по нему уже сделана или делается, и
    заставлять мессенджер присылать его снова незачем.

    Переполнению и остановке — 503. Это осознанный выбор в пользу 503 против
    «200 и молча выбросить»: мессенджер повторит доставку через несколько
    секунд, и пользователь получит настоящий ответ с небольшой задержкой
    вместо бесследно пропавшего сообщения. Лимит при этом не списывается —
    списание происходит только по факту доставленного результата.
    """
    match outcome:
        case Outcome.ACCEPTED | Outcome.DUPLICATE:
            return HTTPStatus.OK
        case Outcome.MALFORMED:
            return HTTPStatus.BAD_REQUEST
        case Outcome.OVERLOADED | Outcome.STOPPING:
            return HTTPStatus.SERVICE_UNAVAILABLE
