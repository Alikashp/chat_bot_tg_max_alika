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
