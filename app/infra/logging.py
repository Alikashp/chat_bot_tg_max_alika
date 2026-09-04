"""Структурное логирование.

JSON в stdout — Railway собирает stdout как есть (§5 задания).

Событие кладётся в поле ``message``, а не в структлоговское ``event``. Это не
косметика: Railway разбирает JSON-строку и показывает в ленте именно
``message``. Со «своим» именем поля лента показывала уровень и пустую строку
вместо текста — то есть логов в проде фактически не было.

Главное здесь — процессор ``drop_sensitive``. §3.5 запрещает попадание в логи
содержимого сообщений и токенов. Полагаться на дисциплину «не логируй лишнего»
нельзя: рано или поздно кто-нибудь передаст в лог весь объект апдейта. Поэтому
запрет реализован механически — опасные ключи вырезаются на выходе, независимо
от того, кто и как их туда положил.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

#: Ключи, значения которых не должны попасть в логи ни при каких условиях.
#: User ID логировать можно и нужно (§3.5), поэтому его здесь нет.
SENSITIVE_KEYS = frozenset(
    {
        "text",
        "caption",
        "prompt",
        "instruction",
        "message",
        "content",
        "token",
        "api_key",
        "secret",
        "authorization",
        # Почта покупателя. В логах ей делать нечего: это персональные
        # данные, а нужна она ровно в одном месте — в чеке.
        "email",
        "password",
        "photo",
        "image",
    }
)

REDACTED = "[вырезано]"


def drop_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Заменяет значения чувствительных ключей заглушкой.

    Сравнение по подстроке: ``llm_api_key`` и ``telegram_bot_token`` тоже
    должны отсекаться, а не только точные совпадения.
    """
    for key in list(event_dict):
        lowered = key.lower()
        if any(marker in lowered for marker in SENSITIVE_KEYS):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Настраивает structlog и стандартный logging.

    ``json_output=False`` включает читаемый вывод — удобно локально, в проде
    всегда JSON.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level),
    )

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            drop_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Строго после drop_sensitive: иначе имя события попадёт под
            # правило «ключ содержит message» и вырежется само себя.
            structlog.processors.EventRenamer(to="message", replace_by="_message"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Возвращает именованный логгер."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
