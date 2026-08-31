"""Точка входа: сборка зависимостей и запуск сервиса.

Здесь и только здесь конкретные реализации соединяются с портами. Всё, что
выше по стеку, знает лишь протоколы: сценарии не подозревают ни про aiogram,
ни про httpx, ни про PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import web
from maxapi import Bot as MaxBot
from maxapi.client.default import DefaultConnectionProperties
from maxapi.enums.update import UpdateType as MaxUpdateType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.ai.http import create_client
from app.adapters.ai.images import OpenAIImages
from app.adapters.ai.resilience import ProviderPolicy, ResilientCaller
from app.adapters.ai.text import OpenAICompatibleLLM
from app.adapters.max import router as max_router
from app.adapters.max.intake import dedup_key as max_dedup_key
from app.adapters.max.messenger import MaxMessenger
from app.adapters.payments.yookassa import YooKassaPayments, order_id_of
from app.adapters.storage.migrations import upgrade_to_head_async
from app.adapters.storage.postgres import PostgresStorage, create_engine
from app.adapters.telegram import router as telegram_router
from app.adapters.telegram.intake import dedup_key
from app.adapters.telegram.messenger import TelegramMessenger
from app.adapters.telegram.stars import TelegramStars
from app.config import Settings, get_settings
from app.core.models import Chat, MessengerKind
from app.core.referral import MAX_HOST, TELEGRAM_HOST
from app.core.scenarios import payments
from app.core.scenarios.deps import Deps, Session
from app.core.settings import CoreSettings
from app.infra.antiflood import FloodGuard
from app.infra.dedup import Deduplicator
from app.infra.logging import configure_logging, get_logger
from app.infra.queue import JobQueue
from app.infra.retry import RetryPolicy
from app.infra.server import (
    MAX_SECRET_HEADER,
    TELEGRAM_SECRET_HEADER,
    Outcome,
    Webhook,
    create_app,
)
from app.ports.payments import StarsPayments

logger = get_logger(__name__)

#: Таймаут вызовов к Telegram Bot API. Без явного значения aiogram ждёт 60 с
#: (§3.4.6 требует явный таймаут на каждый HTTP-вызов).
TELEGRAM_API_TIMEOUT = 15.0

#: Что бот получает на вебхук. Чем уже список, тем меньше мусорного трафика.
#: callback_query здесь обязателен: без него нажатия inline-кнопок просто не
#: доходят, и это выглядит как «кнопки не работают».
#: pre_checkout_query здесь так же обязателен, как callback_query: без него
#: Telegram не дождётся ответа и не проведёт оплату звёздами.
ALLOWED_UPDATES = ["message", "callback_query", "pre_checkout_query"]

#: То же для MAX. Три типа против четырнадцати возможных
#: (docs/research.md §1.4): остальные события боту не нужны.
MAX_UPDATE_TYPES = [
    MaxUpdateType.MESSAGE_CREATED,
    MaxUpdateType.MESSAGE_CALLBACK,
    MaxUpdateType.BOT_STARTED,
]


@dataclass(slots=True)
class MaxWiring:
    """Собранный MAX-бот. Отсутствует целиком, если MAX не настроен."""

    bot: MaxBot
    http: httpx.AsyncClient
    deps: Deps
    handle: Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class Settlement:
    """Приём уведомлений об оплате картой. None, если карты не настроены."""

    handle: Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class Wiring:
    """Собранное приложение и всё, что при остановке нужно закрыть."""

    bot: Bot
    dispatcher: Dispatcher
    engine: AsyncEngine
    http_clients: tuple[httpx.AsyncClient, ...]
    max: MaxWiring | None
    settlement: Settlement | None


def _payment_notice_key(notification: dict[str, Any]) -> str | None:
    """Ключ дедупликации уведомления об оплате — наш идентификатор заказа."""
    order_id = order_id_of(notification)
    return f"yk:{order_id}" if order_id is not None else None


def _utc_now() -> datetime:
    """Часы приложения. Всегда с зоной: наивное время ломает счёт суток."""
    return datetime.now(UTC)


def build_intake(
    queue: JobQueue[dict[str, Any]],
    dedup: Deduplicator,
    *,
    messenger: str,
    key_of: Callable[[dict[str, Any]], str | None],
) -> Callable[[dict[str, Any]], Outcome]:
    """Собирает функцию приёма обновления для HTTP-обработчика.

    Порядок проверок важен. Сначала дедупликация, потом очередь: повтор не
    должен занимать место в очереди, иначе при ретраях мессенджера ёмкость
    выедается копиями одного и того же обновления.

    Ключ вычисляет адаптер: у Telegram есть сквозной update_id, у MAX его нет
    и ключ составной (docs/research.md §1.4).
    """

    def submit(raw_update: dict[str, Any]) -> Outcome:
        key = key_of(raw_update)
        if key is None:
            logger.warning("update_without_id", messenger=messenger)
            return Outcome.MALFORMED

        if not dedup.is_new(key):
            logger.info("update_duplicate", messenger=messenger)
            return Outcome.DUPLICATE

        if not queue.accepting:
            return Outcome.STOPPING

        if not queue.submit(raw_update):
            return Outcome.OVERLOADED

        return Outcome.ACCEPTED

    return submit


def build_providers(
    settings: Settings,
) -> tuple[OpenAICompatibleLLM, OpenAIImages, tuple[httpx.AsyncClient, ...]]:
    """Создаёт провайдеров ИИ вместе с их обвязкой.

    Клиентов два, а не один: у текста и картинок разные таймауты, и общий
    клиент означал бы либо минуту ожидания текста, либо обрыв картинки на
    середине.
    """
    llm_client = create_client(settings.llm_timeout_seconds)
    image_client = create_client(settings.image_timeout_seconds)

    llm = OpenAICompatibleLLM(
        llm_client,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        caller=ResilientCaller(
            "llm",
            ProviderPolicy(
                rate=settings.llm_rate_per_second,
                burst=settings.llm_burst,
                concurrency=settings.llm_concurrency,
            ),
        ),
        system_prompt=settings.llm_system_prompt,
        max_tokens=settings.llm_max_tokens,
    )

    images = OpenAIImages(
        image_client,
        base_url=settings.image_base_url,
        api_key=settings.images_api_key,
        caller=ResilientCaller(
            "images",
            ProviderPolicy(
                rate=settings.image_rate_per_second,
                burst=settings.image_burst,
                concurrency=settings.image_concurrency,
                retry=RetryPolicy(attempts=settings.image_retry_attempts),
                # Таймаут не значит, что на той стороне ничего не нарисовали.
                # Повтор рискует оплатить одну картинку дважды, а пользователь
                # от отказа не теряет ничего: лимит списывается только за
                # доставленный результат.
                retry_on_timeout=False,
            ),
        ),
        model=settings.image_model,
        size=settings.image_size,
    )
    return llm, images, (llm_client, image_client)


def build_cards(
    settings: Settings, *, return_url: str
) -> tuple[YooKassaPayments | None, httpx.AsyncClient | None]:
    """Провайдер оплаты картой, если он настроен.

    Без ключей возвращает None: бот при этом работает, но платить можно
    только звёздами, а без них — честная заглушка «оплата скоро».
    """
    if not settings.cards_enabled:
        logger.info("cards_not_configured")
        return None, None

    client = create_client(settings.yookassa_timeout_seconds)
    return (
        YooKassaPayments(
            client,
            base_url=settings.yookassa_base_url,
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            return_url=return_url,
        ),
        client,
    )


def build_core_settings(
    settings: Settings,
    bot_username: str,
    *,
    referral_link_host: str,
    show_user_number: bool = False,
) -> CoreSettings:
    """Продуктовые настройки ядра из настроек приложения.

    У каждого мессенджера свой экземпляр: имя бота, хост ссылки и показ
    номера у них разные, всё остальное одинаковое.
    """
    return CoreSettings(
        bot_username=bot_username,
        referral_link_host=referral_link_host,
        show_user_number=show_user_number,
        model_economy=settings.model_economy,
        model_standard=settings.model_standard,
        dialog_max_turns=settings.dialog_max_turns,
        max_photo_bytes=settings.max_photo_bytes,
        stars_markup=settings.stars_markup,
        rub_per_star=settings.rub_per_star,
        subscription_days=settings.subscription_days,
        offer_url=settings.offer_url,
        privacy_url=settings.privacy_url,
        docs_version=settings.docs_version,
    )


async def build_wiring(settings: Settings) -> Wiring:
    """Соединяет реализации с портами и собирает диспетчер бота."""
    engine = await _prepare_database(settings)

    bot = Bot(
        token=settings.telegram_bot_token,
        session=AiohttpSession(timeout=TELEGRAM_API_TIMEOUT),
    )
    # Имя бота спрашиваем у самого Telegram, а не заводим переменную
    # окружения: лишняя переменная — это лишний способ разойтись с
    # действительностью, а в ней собираются реферальные ссылки.
    me = await bot.get_me()
    if me.username is None:
        raise RuntimeError("у бота нет username — реферальные ссылки не собрать")
    logger.info("bot_identified", bot_id=me.id)

    llm, images, http_clients = build_providers(settings)
    storage = PostgresStorage(engine)
    # Ограничитель одновременных задач общий на оба мессенджера: ключ
    # содержит внутренний id пользователя, а он у мессенджеров разный.
    guard = FloodGuard(limit=settings.flood_limit_per_user)

    # Возвращаем человека в переписку с ботом, а не на посторонний сайт.
    cards, cards_client = build_cards(
        settings,
        return_url=settings.yookassa_return_url or f"{TELEGRAM_HOST}/{me.username}",
    )
    if cards_client is not None:
        http_clients = (*http_clients, cards_client)

    def build_deps(
        messenger: Any,
        core_settings: CoreSettings,
        *,
        stars: StarsPayments | None = None,
    ) -> Deps:
        """Одни и те же зависимости, разный мессенджер и его настройки."""
        return Deps(
            storage=storage,
            messenger=messenger,
            llm=llm,
            images=images,
            settings=core_settings,
            logger=get_logger("scenarios"),
            guard=guard,
            cards=cards,
            stars=stars,
            now=_utc_now,
        )

    deps = build_deps(
        TelegramMessenger(bot),
        build_core_settings(settings, me.username, referral_link_host=TELEGRAM_HOST),
        # Звёзды бывают только в Telegram: в MAX такого механизма нет.
        stars=TelegramStars(bot),
    )

    max_wiring = await _build_max(settings, build_deps)

    by_messenger = {MessengerKind.TELEGRAM: deps}
    if max_wiring is not None:
        by_messenger[MessengerKind.MAX] = max_wiring.deps

    return Wiring(
        bot=bot,
        dispatcher=telegram_router.build_dispatcher(deps),
        engine=engine,
        http_clients=http_clients,
        max=max_wiring,
        settlement=(
            _build_settlement(cards, by_messenger) if cards is not None else None
        ),
    )


def _build_settlement(
    cards: YooKassaPayments,
    by_messenger: dict[MessengerKind, Deps],
) -> Settlement:
    """Приём уведомлений об оплате картой.

    Уведомление здесь — только повод переспросить: статус читается у ЮKassa
    нашим ключом. Иначе любой, кто узнал адрес вебхука, выдавал бы себе
    подписки: их уведомления не подписаны ничем.

    Отвечать человеку надо в тот мессенджер, из которого он пришёл, поэтому
    зависимости выбираются по самому пользователю, а не по тому, куда
    постучались.
    """
    any_deps = next(iter(by_messenger.values()))

    async def handle(notification: dict[str, Any]) -> None:
        order_id = order_id_of(notification)
        if order_id is None:
            logger.warning("payment_notice_without_order")
            return

        order = await any_deps.storage.get_payment(order_id)
        if order is None or order.external_id is None:
            logger.warning("payment_notice_unknown_order")
            return

        if not await cards.is_paid(order.external_id, expected_rub=order.amount):
            logger.info("payment_notice_not_paid")
            return

        user = await any_deps.storage.get_user_by_id(order.user_id)
        if user is None:
            logger.error("payment_user_missing", user_id=int(order.user_id))
            return

        deps = by_messenger.get(user.messenger)
        if deps is None:
            # Человек пришёл из мессенджера, который сейчас выключен. Тариф
            # всё равно выдаём — деньги получены; не скажем только об этом.
            logger.warning("payment_messenger_disabled", user_id=int(user.id))
            deps = any_deps

        confirmed = await payments.confirm(deps, order_id)
        if confirmed is None:
            return

        session = Session(
            user=user,
            chat=Chat(messenger=user.messenger, chat_id=user.external_id),
            day=deps.today(),
            now=deps.now(),
        )
        await payments.announce(deps, session, confirmed)

    return Settlement(handle=handle)


async def _build_max(
    settings: Settings,
    build_deps: Callable[[Any, CoreSettings], Deps],
) -> MaxWiring | None:
    """Собирает MAX-бота, если он настроен.

    Без токена возвращает None, и сервис работает только с Telegram. Это не
    заглушка, а выключатель: публиковать бота в MAX могут только
    верифицированные юрлица (docs/research.md §1.8), и ронять работающий
    Telegram из-за организационной задержки было бы неправильно.
    """
    if not settings.max_enabled:
        logger.info("max_not_configured")
        return None

    bot = MaxBot(
        token=settings.max_bot_token,
        # Диспетчер и роутеры библиотеки не используем: маршрутизация у нас
        # одна и живёт в core. Отключаем и то, что библиотека делает сама.
        auto_requests=False,
        auto_check_subscriptions=False,
        default_connection=DefaultConnectionProperties(
            timeout=settings.max_api_timeout_seconds,
            sock_connect=5,
            # Повторы и предохранитель у нас свои, из infra/. Два независимых
            # слоя повторов множат нагрузку на лежащий сервис.
            max_retries=0,
        ),
    )

    me = await bot.get_me()
    if me.username is None:
        raise RuntimeError("у MAX-бота нет username — реферальные ссылки не собрать")
    logger.info("max_bot_identified", bot_id=me.user_id)

    http = create_client(settings.max_api_timeout_seconds)
    deps = build_deps(
        MaxMessenger(bot, http),
        build_core_settings(
            settings,
            me.username,
            referral_link_host=MAX_HOST,
            # В MAX username есть не у всех, и без номера опознать
            # написавшего в поддержку нечем.
            show_user_number=True,
        ),
    )

    async def handle(raw_update: dict[str, Any]) -> None:
        await max_router.handle_update(deps, raw_update)

    return MaxWiring(bot=bot, http=http, deps=deps, handle=handle)


async def _register_webhook(bot: Bot, settings: Settings) -> None:
    """Регистрирует вебхук при старте.

    Вызывается на каждом запуске: адрес на Railway может смениться, а
    setWebhook идемпотентен. Ожидающие обновления не сбрасываем — их
    доставят после перезапуска.
    """
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
    logger.info("webhook_registered", messenger="telegram")


async def _subscribe_max(bot: MaxBot, settings: Settings) -> None:
    """Подписывает вебхук MAX при старте.

    Перед подпиской снимаем чужие: MAX хранит список подписок, и повторные
    выкатки с новым адресом накапливали бы их. Каждая лишняя подписка — это
    доставка того же события ещё раз, а значит лишняя картинка за наши деньги.
    Дедупликация такое поймает, но полагаться на неё, когда причину можно
    убрать, неправильно.
    """
    target = settings.max_webhook_url
    existing = await bot.get_subscriptions()
    for subscription in existing.subscriptions:
        if subscription.url != target:
            await bot.unsubscribe_webhook(url=subscription.url)
            logger.info("max_webhook_removed", messenger="max")

    await bot.subscribe_webhook(
        url=target,
        update_types=MAX_UPDATE_TYPES,
        secret=settings.max_secret,
    )
    logger.info("webhook_registered", messenger="max")


async def _prepare_database(settings: Settings) -> AsyncEngine:
    """Накатывает миграции и проверяет связь.

    Проверка сразу, а не у первого пользователя: неверный адрес базы должен
    валить старт, а не превращаться в поток ошибок в чате.
    """
    if settings.run_migrations_on_start:
        await upgrade_to_head_async(settings.database_url)
        logger.info("migrations_applied")

    engine = create_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    logger.info("database_ready")
    return engine


async def run() -> None:
    """Поднимает сервис и работает до SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env == "production")

    wiring = await build_wiring(settings)

    async def handle_update(raw_update: dict[str, Any]) -> None:
        """Обрабатывает одно обновление вне HTTP-запроса."""
        await wiring.dispatcher.feed_raw_update(wiring.bot, raw_update)

    queue: JobQueue[dict[str, Any]] = JobQueue(
        "telegram-updates",
        handle_update,
        capacity=settings.queue_capacity,
        workers=settings.queue_workers,
    )
    dedup = Deduplicator(
        ttl_seconds=settings.dedup_ttl_seconds,
        max_keys=settings.dedup_max_keys,
    )
    queues = [queue]
    webhooks = [
        Webhook(
            messenger="telegram",
            path=settings.telegram_webhook_path,
            secret_header=TELEGRAM_SECRET_HEADER,
            secret=settings.telegram_webhook_secret,
            submit=build_intake(queue, dedup, messenger="telegram", key_of=dedup_key),
        )
    ]

    if wiring.settlement is not None:
        # Уведомления об оплате идут своей очередью: они приходят редко, но
        # каждое делает запрос к ЮKassa, и мешать их с потоком сообщений
        # незачем. Ключ дедупликации — наш заказ: ЮKassa повторяет
        # уведомление несколько раз, а работа по нему одна.
        settle_queue: JobQueue[dict[str, Any]] = JobQueue(
            "payment-notices",
            wiring.settlement.handle,
            capacity=settings.queue_capacity,
            workers=4,
        )
        queues.append(settle_queue)
        webhooks.append(
            Webhook(
                messenger="yookassa",
                path=settings.yookassa_webhook_path,
                # Уведомления ЮKassa не подписаны ничем: ни секретом, ни
                # подписью тела. Принимать их можно только потому, что само
                # уведомление ничего не решает — статус мы переспрашиваем у
                # провайдера своим ключом.
                secret_header=None,
                secret=None,
                submit=build_intake(
                    settle_queue,
                    dedup,
                    messenger="yookassa",
                    key_of=_payment_notice_key,
                ),
            )
        )

    if wiring.max is not None:
        # Очередь у MAX своя. Общая была бы проще, но тогда наплыв в одном
        # мессенджере съедал бы ёмкость у другого — а мессенджеры независимы,
        # и падать вместе им незачем.
        max_queue: JobQueue[dict[str, Any]] = JobQueue(
            "max-updates",
            wiring.max.handle,
            capacity=settings.queue_capacity,
            workers=settings.queue_workers,
        )
        queues.append(max_queue)
        webhooks.append(
            Webhook(
                messenger="max",
                path=settings.max_webhook_path,
                secret_header=MAX_SECRET_HEADER,
                secret=settings.max_secret,
                submit=build_intake(
                    max_queue, dedup, messenger="max", key_of=max_dedup_key
                ),
            )
        )

    def health() -> dict[str, Any]:
        accepting = all(each.accepting for each in queues)
        return {
            "status": "ok" if accepting else "shutting_down",
            "queues": [
                {
                    "name": stats.name,
                    "capacity": stats.capacity,
                    "workers": stats.workers,
                    "pending": stats.pending,
                    "in_flight": stats.in_flight,
                    "accepted": stats.accepted,
                    "rejected": stats.rejected,
                    "failed": stats.failed,
                }
                for stats in (each.stats() for each in queues)
            ],
            "dedup_keys": len(dedup),
            "database": "ready",
            "max": "ready" if wiring.max is not None else "not_configured",
            "cards": "ready" if wiring.settlement is not None else "not_configured",
            "documents": "ready" if settings.documents_ready else "not_configured",
        }

    app = create_app(webhooks=webhooks, health=health)

    for each in queues:
        each.start()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)  # noqa: S104

    try:
        await site.start()
        logger.info(
            "server_started",
            port=settings.port,
            capacity=settings.queue_capacity,
            workers=settings.queue_workers,
        )

        # Порядок именно такой: сокет уже слушает, когда мы говорим Telegram
        # присылать сюда обновления. Наоборот — значит некоторое время
        # получать обновления на адрес, который ещё никто не слушает.
        #
        # Если регистрация не удалась, поднимать сервис бессмысленно: бот не
        # получит ни одного сообщения. Падаем, и Railway перезапускает нас по
        # restartPolicy из railway.json.
        await _register_webhook(wiring.bot, settings)
        if wiring.max is not None:
            await _subscribe_max(wiring.max.bot, settings)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("shutdown_started")
    finally:
        # Порядок важен: сначала перестаём брать новую работу, потом закрываем
        # приём соединений, и только потом ждём текущие задачи. Если сделать
        # наоборот, между остановкой сокета и запретом на задачи успеет
        # проскочить новое обновление.
        for each in queues:
            each.stop_accepting()
        await site.stop()
        abandoned = 0
        for each in queues:
            abandoned += await each.drain(settings.shutdown_timeout_seconds)
        await runner.cleanup()
        await wiring.bot.session.close()
        for client in wiring.http_clients:
            await client.aclose()
        if wiring.max is not None:
            await wiring.max.bot.close_session()
            await wiring.max.http.aclose()
        await wiring.engine.dispose()

        logger.info("shutdown_complete", abandoned=abandoned)


def main() -> None:
    """Запускает приложение."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
