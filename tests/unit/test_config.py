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
    "database_url": "postgresql://bot:bot@localhost:5432/botdb",
    "llm_api_key": "sk-test",
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


# --- Переключатели на отдельный прикол -----------------------------------


def test_a_preset_may_name_its_own_model_and_quality() -> None:
    """Переключать модель приколу надо уметь без выкладки."""
    settings = Settings.model_validate(
        {
            **VALID_ENV,
            "preset_models": {"figurine": "gpt-image-1-mini"},
            "preset_qualities": {"id_photo": "high"},
        }
    )

    assert settings.preset_models == {"figurine": "gpt-image-1-mini"}
    assert settings.preset_qualities == {"id_photo": "high"}


def test_a_typo_in_the_quality_stops_the_start() -> None:
    """Иначе это 400 на каждом таком приколе, и видно только по отказу провайдера.

    Имена значений короткие и похожие, а ошибка в них ничем больше не
    проявляется: картинка просто перестаёт получаться.
    """
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**VALID_ENV, "preset_qualities": {"id_photo": "ultra"}}
        )


def test_premium_emoji_are_read_from_the_environment() -> None:
    """Идентификаторы подбирают глазами — менять их без выкладки надо уметь."""
    settings = Settings.model_validate(
        {**VALID_ENV, "telegram_premium_emoji": {"🔄": "5345906554510012647"}}
    )

    assert settings.telegram_premium_emoji == {"🔄": "5345906554510012647"}


def test_a_premium_emoji_id_that_is_not_a_number_stops_the_start() -> None:
    """Отказ Telegram приходит на отправку: человек не получит вообще ничего."""
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**VALID_ENV, "telegram_premium_emoji": {"🔄": "Загрузка"}}
        )


def test_no_premium_emoji_is_the_normal_case() -> None:
    """Пустой словарь — сообщения уходят ровно как раньше."""
    assert Settings.model_validate(VALID_ENV).telegram_premium_emoji == {}


def test_no_switches_is_the_normal_case() -> None:
    """Пустые словари означают «общая модель и качество тарифа»."""
    settings = Settings.model_validate(VALID_ENV)

    assert settings.preset_models == {}
    assert settings.preset_qualities == {}


def test_a_channel_link_is_accepted() -> None:
    settings = Settings.model_validate(
        {**VALID_ENV, "channel_url": " https://t.me/chatgptbotonline "}
    )

    assert settings.channel_url == "https://t.me/chatgptbotonline"


def test_no_channel_is_the_normal_case() -> None:
    """Пустая ссылка означает «бонуса за подписку нет»."""
    assert Settings.model_validate(VALID_ENV).channel_url == ""


@pytest.mark.parametrize(
    "link",
    [
        # Приглашение в приватный канал: публичного имени нет, проверять
        # подписку не по чему.
        "https://t.me/+AbCdEf",
        "https://vk.com/chatgptbotonline",
        "chatgptbotonline",
    ],
)
def test_a_link_we_cannot_check_stops_the_start(link: str) -> None:
    """Иначе кнопка висела бы и обещала картинки, которых не выдадут.

    Опечатку в ссылке иначе видно только по жалобе человека, который
    подписался и бонуса не получил.
    """
    with pytest.raises(ValidationError):
        Settings.model_validate({**VALID_ENV, "channel_url": link})
