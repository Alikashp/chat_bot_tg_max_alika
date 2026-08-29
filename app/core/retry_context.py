"""Что нужно помнить, чтобы кнопка «🔄 Ещё раз» действительно работала.

В задании таких кнопок четыре: «Повторить» под ошибкой чата (§2.2), «Ещё раз»
и «Поделиться» под картинкой (§2.3), «Ещё раз» и «Отправить другу» под
приколом (§2.4). Каждой нужно знать, что именно повторить или чем поделиться,
а в кнопку это не положишь: callback_data в Telegram ограничен 64 байтами, а
описание картинки бывает длиной в абзац.

Поэтому контекст хранится у пользователя в базе. Ссылки на фото — строки,
которые понимает адаптер своего мессенджера (в Telegram file_id, в MAX свой
идентификатор): ядро в них не заглядывает, а только передаёт обратно.

Контекст один на пользователя и перезаписывается: «ещё раз» осмысленно
относится к последнему результату, а не к любому из прошлых.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum


class RetryKind(StrEnum):
    """Что повторяем."""

    CHAT = "chat"
    IMAGE = "image"
    PRESET = "preset"


@dataclass(frozen=True, slots=True)
class RetryContext:
    """Всё, что нужно для повтора и для шеринга последнего результата."""

    kind: RetryKind
    #: Сообщение пользователя или описание картинки.
    prompt: str = ""
    #: Какой прикол применяли.
    preset_id: str | None = None
    #: Исходное фото пользователя — чтобы применить прикол ещё раз.
    source_photo: str | None = None
    #: Готовая картинка — чтобы переслать её с подписью и ссылкой.
    result_photo: str | None = None

    def encode(self) -> str:
        """Сериализует контекст для хранилища."""
        return json.dumps(
            {**asdict(self), "kind": self.kind.value},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def decode(raw: str | None) -> RetryContext | None:
    """Восстанавливает контекст. None, если его нет или он испорчен.

    Испорченный контекст — не повод падать: он мог остаться от прошлой версии
    формата, а пользователю в этом случае достаточно показать, что повторять
    нечего, и предложить начать заново.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    try:
        kind = RetryKind(payload["kind"])
    except (KeyError, ValueError):
        return None

    return RetryContext(
        kind=kind,
        prompt=_text(payload.get("prompt")) or "",
        preset_id=_text(payload.get("preset_id")),
        source_photo=_text(payload.get("source_photo")),
        result_photo=_text(payload.get("result_photo")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
