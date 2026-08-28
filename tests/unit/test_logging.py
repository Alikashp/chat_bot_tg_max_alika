"""§3.5: в логах не должно быть содержимого сообщений и токенов."""

from __future__ import annotations

from app.infra.logging import REDACTED, drop_sensitive


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
