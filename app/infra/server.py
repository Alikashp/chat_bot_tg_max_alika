"""HTTP-сервер: приём вебхуков и health-check.

Ключевое требование §3.4.1: обработчик вебхука только валидирует запрос,
ставит обновление в очередь и отвечает. Никакой работы с ИИ внутри
HTTP-запроса — иначе мессенджер отвалится по таймауту и начнёт ретраить,
а картинка рисуется 15 секунд.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from secrets import compare_digest
from typing import Any

from aiohttp import web

from app.infra.logging import get_logger

logger = get_logger(__name__)

#: Заголовки, которыми мессенджеры подписывают каждый запрос вебхука.
#: noqa ниже — ruff видит слово "secret" в именах и считает это захардкоженным
#: паролем; здесь это имена HTTP-заголовков, а не значения секретов.
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105
MAX_SECRET_HEADER = "X-Max-Bot-Api-Secret"  # noqa: S105

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


@dataclass(frozen=True, slots=True)
class Webhook:
    """Один вебхук: куда стучатся, чем подписано, кому отдавать."""

    #: Имя источника — только для логов.
    messenger: str
    path: str
    #: Заголовок, в котором приходит секрет, и сам секрет.
    #:
    #: ``None`` означает, что источник не подписывает свои запросы вовсе —
    #: так устроены уведомления ЮKassa. Принимать такие уведомления можно
    #: только там, где само уведомление ничего не решает: у нас оно лишь
    #: повод переспросить провайдера по нашему ключу (см. CardPayments).
    secret_header: str | None
    secret: str | None
    submit: UpdateSubmitter


def create_app(
    *,
    webhooks: Sequence[Webhook],
    health: HealthProvider,
) -> web.Application:
    """Собирает aiohttp-приложение.

    Вебхуков может быть несколько: один сервис обслуживает и Telegram, и MAX.
    Общий у них только этот обработчик — проверить подпись, разобрать JSON,
    поставить в очередь; всё различие спрятано в submit конкретного адаптера.
    """
    app = web.Application(client_max_size=MAX_WEBHOOK_BODY_BYTES)

    def make_handler(webhook: Webhook) -> Callable[[web.Request], Any]:
        async def handler(request: web.Request) -> web.Response:
            if webhook.secret_header is not None and not _is_authorized(
                request, webhook.secret_header, webhook.secret or ""
            ):
                # Не подсказываем, что именно не так: это чужой запрос.
                logger.warning("webhook_rejected", messenger=webhook.messenger)
                return web.Response(status=HTTPStatus.FORBIDDEN)

            try:
                update = await request.json()
            except ValueError:
                logger.warning("webhook_bad_json", messenger=webhook.messenger)
                return web.Response(status=HTTPStatus.BAD_REQUEST)

            if not isinstance(update, dict):
                logger.warning("webhook_bad_payload", messenger=webhook.messenger)
                return web.Response(status=HTTPStatus.BAD_REQUEST)

            return web.Response(status=_status_for(webhook.submit(update)))

        return handler

    async def health_check(_request: web.Request) -> web.Response:
        payload = health()
        status = (
            HTTPStatus.OK
            if payload.get("status") == "ok"
            else HTTPStatus.SERVICE_UNAVAILABLE
        )
        return web.json_response(payload, status=status)

    for webhook in webhooks:
        app.router.add_post(webhook.path, make_handler(webhook))
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
