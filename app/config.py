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
    #: Пока необязательна. Продуктовые сценарии подключаются к боту на фазе 4,
    #: и до тех пор бот живёт без хранилища. С фазы 4 переменная станет
    #: обязательной: подписки и лимиты обязаны переживать выкатку, а без базы
    #: они не переживают даже перезапуск.
    database_url: str | None = None

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
