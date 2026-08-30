"""§3.5: в логах не должно быть содержимого сообщений и токенов."""

from __future__ import annotations

import json

import pytest

from app.infra.logging import REDACTED, configure_logging, drop_sensitive, get_logger


def test_message_text_is_removed() -> None:
    result = drop_sensitive(None, "info", {"event": "reply", "text": "секрет"})

    assert result["text"] == REDACTED


def test_token_like_keys_are_removed_by_substring() -> None:
    """Отсекаться должны и составные имена, а не только точные совпадения."""
    result = drop_sensitive(
        None,
        "info",
        {
            "telegram_bot_token": "123:abc",
            "llm_api_key": "sk-xxx",
            "webhook_secret": "s3cret",
        },
    )

    assert all(value == REDACTED for value in result.values())


def test_user_id_is_kept() -> None:
    """User ID логировать разрешено — по нему разбираются с обращениями."""
    result = drop_sensitive(None, "info", {"event": "reply", "user_id": 42})

    assert result["user_id"] == 42


def test_event_name_is_kept() -> None:
    result = drop_sensitive(None, "info", {"event": "webhook_registered"})

    assert result["event"] == "webhook_registered"


def test_the_event_reaches_the_log_as_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Railway показывает в ленте поле message и ничего больше.

    Проверка появилась после настоящего сбоя: в проде лента показывала уровень
    и пустую строку, потому что событие лежало в поле event. Логи были, и их
    как будто не было — искать причину падения картинок было нечем.
    """
    configure_logging("INFO", json_output=True)

    get_logger("test").warning("image_failed", user_id=42)

    written = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert written["message"] == "image_failed"
    assert written["user_id"] == 42
    assert written["level"] == "warning"


def test_a_field_named_message_does_not_shadow_the_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Поле message от вызывающего кода вырезается и не занимает чужое место."""
    configure_logging("INFO", json_output=True)

    get_logger("test").warning("llm_failed", message="секретная переписка")

    output = capsys.readouterr().out
    written = json.loads(output.strip().splitlines()[-1])
    assert written["message"] == "llm_failed"
    assert "секретная переписка" not in output
