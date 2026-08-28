"""Конфигурация обязана падать на старте, а не работать наполовину."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings

VALID_ENV: dict[str, Any] = {
    "public_url": "https://bot.example.com",
    "telegram_bot_token": "123456:token",
    "telegram_webhook_secret": "a" * 32,
}


def test_valid_settings_are_accepted() -> None:
    settings = Settings.model_validate(VALID_ENV)

    assert settings.port == 8080
    assert settings.app_env == "production"


@pytest.mark.parametrize("missing", sorted(VALID_ENV))
def test_missing_required_variable_fails(missing: str) -> None:
    """Без любой из обязательных переменных приложение не должно подняться."""
    env = {key: value for key, value in VALID_ENV.items() if key != missing}

    with pytest.raises(ValidationError):
        Settings.model_validate(env)


def test_plain_http_public_url_is_rejected() -> None:
    """Оба мессенджера принимают вебхуки только по HTTPS."""
    with pytest.raises(ValidationError):
        Settings.model_validate({**VALID_ENV, "public_url": "http://bot.example.com"})


def test_webhook_secret_with_forbidden_characters_is_rejected() -> None:
    """Алфавит MAX уже телеграмного: подчёркивание в нём не разрешено."""
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**VALID_ENV, "telegram_webhook_secret": "a" * 31 + "_"}
        )


def test_short_webhook_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**VALID_ENV, "telegram_webhook_secret": "short"})


def test_webhook_url_is_built_from_public_url() -> None:
    settings = Settings.model_validate(
        {**VALID_ENV, "public_url": "https://bot.example.com/"}
    )

    assert settings.telegram_webhook_url == "https://bot.example.com/webhook/telegram"


def test_webhook_path_does_not_contain_secret() -> None:
    """Путь попадает в логи прокси, секрет там не место."""
    settings = Settings.model_validate(VALID_ENV)

    assert settings.telegram_webhook_secret not in settings.telegram_webhook_path
