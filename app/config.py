"""Конфигурация приложения.

Все настройки приходят из переменных окружения и проверяются на старте.
Если обязательной переменной не хватает или значение не проходит валидацию,
приложение падает при запуске — это осознанное поведение (см. §3.1 задания):
лучше не подняться совсем, чем работать наполовину.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.prompt import SYSTEM_PROMPT

#: Секрет вебхука MAX допускает только эти символы (см. docs/research.md §1.3).
_MAX_SECRET_ALPHABET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"
)


class Settings(BaseSettings):
    """Настройки приложения, собранные из переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Общие ---------------------------------------------------------
    app_env: Literal["local", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    #: Порт HTTP-сервера. На Railway приходит из окружения как PORT.
    port: Annotated[int, Field(ge=1, le=65535)] = 8080

    #: Публичный адрес сервиса. Из него собирается URL вебхука при старте.
    public_url: HttpUrl

    # --- База данных ----------------------------------------------------

    #: Строка подключения к PostgreSQL. На Railway приходит из переменной
    #: DATABASE_URL, которую добавляет плагин базы.
    #:
    #: Обязательна с фазы 4: лимиты, бонусы и рефералы обязаны переживать
    #: выкатку, а без базы они не переживают даже перезапуск. Приложение,
    #: которое поднялось без хранилища, молча раздавало бы всем бесконечный
    #: бесплатный тариф — падение на старте честнее.
    database_url: Annotated[str, Field(min_length=1)]

    #: Применять миграции при старте. Сервис однопроцессный (§5), так что
    #: гонки двух одновременных накатов быть не может. Выключается на случай,
    #: если миграции захочется прогонять отдельным шагом.
    run_migrations_on_start: bool = True

    # --- Очередь обновлений --------------------------------------------

    #: Сколько обновлений помещается в очередь. За этим пределом приходит
    #: отказ, и мессенджер повторит доставку позже. Ограничение существует
    #: затем, чтобы под наплывом получить честный отказ, а не медленную
    #: деградацию до исчерпания памяти.
    queue_capacity: Annotated[int, Field(ge=1, le=100_000)] = 1_000

    #: Сколько обновлений обрабатывается одновременно.
    queue_workers: Annotated[int, Field(ge=1, le=256)] = 16

    # --- Дедупликация ---------------------------------------------------

    #: Сколько помним ключ обновления. Telegram повторяет доставку в пределах
    #: минут, так что десяти минут с запасом хватает.
    dedup_ttl_seconds: Annotated[float, Field(gt=0, le=3600)] = 600.0

    #: Потолок числа запомненных ключей: кэш без предела — это утечка,
    #: которая проявится через неделю аптайма, а не на тестах.
    dedup_max_keys: Annotated[int, Field(ge=1_000, le=5_000_000)] = 100_000

    #: Сколько секунд даём воркерам доработать после SIGTERM.
    #: Должно быть заведомо меньше окна Railway
    #: (RAILWAY_DEPLOYMENT_DRAINING_SECONDS), иначе нас убьют по SIGKILL
    #: посреди работы.
    shutdown_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0

    # --- Telegram ------------------------------------------------------
    telegram_bot_token: Annotated[str, Field(min_length=1)]

    #: Проверяется в заголовке X-Telegram-Bot-Api-Secret-Token на каждом
    #: запросе вебхука. Telegram разрешает 1-256 символов из A-Z a-z 0-9 _ -
    telegram_webhook_secret: Annotated[str, Field(min_length=16, max_length=256)]

    # --- Провайдер текста ------------------------------------------------

    #: Адрес OpenAI-совместимого API. Меняется на любой шлюз с тем же
    #: форматом /chat/completions — код при этом не трогается.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: Annotated[str, Field(min_length=1)]

    #: Модели по классам тарифа. Пользователю не показываются и им не
    #: выбираются (§2.2), но сменить их надо уметь без выкладки кода.
    model_economy: str = "gpt-5-mini"
    model_standard: str = "gpt-5"

    #: Потолок длины ответа. Ограничение прежде всего денежное: в мессенджере
    #: всё равно никто не читает простыню на три экрана.
    llm_max_tokens: Annotated[int, Field(ge=64, le=8192)] = 1024

    #: Таймаут вызова (§3.4.6). Больше минуты ждать бессмысленно: человек уже
    #: решил, что бот сломался.
    llm_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 60.0

    #: Инструкция модели. Пользователю не показывается.
    llm_system_prompt: str = SYSTEM_PROMPT

    #: Сколько реплик диалога уходит в каждый запрос.
    #:
    #: Главный рычаг стоимости после выбора модели: контекст отправляется
    #: заново с каждым сообщением, поэтому счёт растёт не линейно, а как
    #: произведение длины окна на число сообщений. Двадцать реплик — это
    #: десять обменов; для бота, где спрашивают и получают ответ, обычно
    #: хватает меньшего.
    dialog_max_turns: Annotated[int, Field(ge=2, le=100)] = 10

    #: Ограничения на провайдера текста: сколько вызовов в секунду, какой
    #: всплеск и сколько одновременно.
    llm_rate_per_second: Annotated[float, Field(gt=0, le=1000)] = 20.0
    llm_burst: Annotated[int, Field(ge=1, le=1000)] = 40
    llm_concurrency: Annotated[int, Field(ge=1, le=256)] = 16

    # --- Провайдер картинок ----------------------------------------------

    image_base_url: str = "https://api.openai.com/v1"

    #: Ключ провайдера картинок. Пустой означает «тот же, что для текста» —
    #: обычный случай, когда провайдер один.
    image_api_key: str = ""

    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"

    #: Картинка рисуется десятки секунд, поэтому таймаут свой и заметно
    #: больше текстового.
    image_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 180.0

    image_rate_per_second: Annotated[float, Field(gt=0, le=1000)] = 5.0
    image_burst: Annotated[int, Field(ge=1, le=1000)] = 10

    #: Одновременных отрисовок меньше, чем текстовых вызовов: картинка держит
    #: провайдера пятнадцать секунд, а сообщение — одну.
    image_concurrency: Annotated[int, Field(ge=1, le=64)] = 8

    #: Сколько раз повторяем вызов картинок. Меньше, чем у текста: каждая
    #: попытка стоит денег.
    image_retry_attempts: Annotated[int, Field(ge=1, le=5)] = 2

    # --- Продуктовые ограничения ------------------------------------------

    #: Потолок размера присланного фото (§3.5). Проверяется до обращения к
    #: провайдеру и до загрузки байтов в память.
    max_photo_bytes: Annotated[int, Field(ge=64 * 1024, le=50 * 1024 * 1024)] = (
        5 * 1024 * 1024
    )

    #: Сколько одновременных задач разрешено одному человеку на каждый вид
    #: работы (§3.4.8). Единица закрывает окно между проверкой остатка и
    #: списанием — двум параллельным запросам одного пользователя в него не
    #: пролезть.
    flood_limit_per_user: Annotated[int, Field(ge=1, le=10)] = 1

    # --- MAX -----------------------------------------------------------

    #: Токен бота MAX от @MasterBot. Пустой означает «MAX выключен»: сервис
    #: поднимается только с Telegram.
    #:
    #: Необязательность здесь осознанная. Публиковать бота в MAX с августа
    #: 2025 могут только верифицированные российские юрлица
    #: (docs/research.md §1.8), и требовать токен на старте значило бы
    #: уронить работающий Telegram из-за организационной задержки.
    max_bot_token: str = ""

    #: Секрет вебхука MAX. Пустой означает «тот же, что у Telegram» —
    #: алфавит у них общий, и второй секрет заводить незачем.
    max_webhook_secret: str = ""

    #: Таймаут вызовов к MAX. Больше телеграмного: отправка картинки там
    #: двухшаговая, с загрузкой файла и паузой на готовность вложения
    #: (docs/research.md §1.7).
    max_api_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0

    # --- Оплата ----------------------------------------------------------

    #: Магазин и ключ ЮKassa. Пустые означают «оплата картой не настроена»:
    #: бот работает, но платить можно только звёздами.
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""

    #: Адрес API. Меняется на тестовый магазин при отладке.
    yookassa_base_url: str = "https://api.yookassa.ru/v3"

    yookassa_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 20.0

    #: Куда ЮKassa вернёт человека после оплаты. Пустой — соберём ссылку на
    #: самого бота: возвращать человека в переписку правильнее, чем на
    #: посторонний сайт.
    yookassa_return_url: str = ""

    #: Сколько рублей стоит одна звезда Telegram. Величина внешняя: курс
    #: задаёт Telegram, и меняется он без нашего участия.
    rub_per_star: Annotated[float, Field(gt=0, le=100)] = 1.6

    #: На сколько дней выдаётся подписка после оплаты.
    subscription_days: Annotated[int, Field(ge=1, le=366)] = 30

    #: Адреса опубликованных оферты и политики обработки данных.
    #: Пока не заданы, оплата в боте не показывается вовсе: брать деньги, не
    #: показав условия, нельзя.
    offer_url: str = ""
    privacy_url: str = ""

    #: Редакция документов — например, «2026-08-31». Пишется в заказ и в лог,
    #: чтобы потом было видно, с чем именно человек соглашался.
    docs_version: str = ""

    #: Наценка на оплату звёздами (§2.8: на 40% выше).
    stars_markup: Annotated[float, Field(ge=1.0, le=3.0)] = 1.4

    @field_validator("public_url")
    @classmethod
    def _require_https(cls, value: HttpUrl) -> HttpUrl:
        """Оба мессенджера принимают вебхуки только по HTTPS.

        Для MAX это жёсткое требование с 25.05.2026 (docs/research.md §1.3),
        Telegram не принимает http никогда. Ловим ошибку на старте, а не
        на первом сообщении пользователя.
        """
        if value.scheme != "https":
            raise ValueError("PUBLIC_URL должен начинаться с https://")
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _validate_webhook_secret(cls, value: str) -> str:
        """Секрет должен подходить обоим мессенджерам сразу.

        Алфавит MAX (A-Z a-z 0-9 -) строго уже телеграмного, поэтому
        проверяем по нему: так один и тот же генератор секретов подойдёт
        и для фазы 7, и не придётся ловить отказ подписки в MAX.
        """
        if not set(value) <= _MAX_SECRET_ALPHABET:
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET допускает только символы A-Z, a-z, 0-9 и дефис"
            )
        return value

    @field_validator("max_webhook_secret")
    @classmethod
    def _validate_max_secret(cls, value: str) -> str:
        """Тот же алфавит, что у телеграмного (docs/research.md §1.3)."""
        if value and not set(value) <= _MAX_SECRET_ALPHABET:
            raise ValueError(
                "MAX_WEBHOOK_SECRET допускает только символы A-Z, a-z, 0-9 и дефис"
            )
        return value

    @property
    def max_enabled(self) -> bool:
        """Поднимать ли MAX. Без токена — нет."""
        return bool(self.max_bot_token)

    @property
    def max_secret(self) -> str:
        """Секрет вебхука MAX с откатом на телеграмный.

        Алфавиты совпадают, и заводить две переменные с одним смыслом —
        верный способ однажды поменять только одну.
        """
        return self.max_webhook_secret or self.telegram_webhook_secret

    @property
    def max_webhook_path(self) -> str:
        return "/webhook/max"

    @property
    def max_webhook_url(self) -> str:
        return f"{str(self.public_url).rstrip('/')}{self.max_webhook_path}"

    @property
    def documents_ready(self) -> bool:
        """Опубликованы ли оферта и политика. Без них оплата не показывается."""
        return bool(self.offer_url and self.privacy_url and self.docs_version)

    @property
    def cards_enabled(self) -> bool:
        """Настроена ли оплата картой."""
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def yookassa_webhook_path(self) -> str:
        """Путь вебхука ЮKassa.

        Секрета у их уведомлений нет вовсе, поэтому путь сделан длинным и
        неугадываемым: он не даёт никаких прав, но избавляет от шума на
        очевидном адресе. Настоящая проверка — переспрос провайдера по нашему
        ключу, см. CardPayments.is_paid.
        """
        return "/webhook/yookassa/notifications"

    @property
    def yookassa_webhook_url(self) -> str:
        return f"{str(self.public_url).rstrip('/')}{self.yookassa_webhook_path}"

    @property
    def images_api_key(self) -> str:
        """Ключ провайдера картинок с откатом на текстовый.

        Провайдер обычно один, и заставлять задавать один и тот же ключ
        дважды — верный способ однажды поменять только один из них.
        """
        return self.image_api_key or self.llm_api_key

    @property
    def telegram_webhook_path(self) -> str:
        """Путь вебхука Telegram.

        Секрет в путь не зашиваем: он передаётся заголовком, а путь попадает
        в логи прокси.
        """
        return "/webhook/telegram"

    @property
    def telegram_webhook_url(self) -> str:
        """Полный URL, который регистрируется в Telegram при старте."""
        return f"{str(self.public_url).rstrip('/')}{self.telegram_webhook_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает настройки, читая окружение один раз за процесс."""
    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
