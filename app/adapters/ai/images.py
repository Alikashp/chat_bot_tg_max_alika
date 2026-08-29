"""Провайдер картинок через Images API.

Один провайдер закрывает оба сценария: рисование по описанию (§2.3) и
переделку присланного фото (§2.4). Второе — не украшение, а причина выбора:
пресеты требуют именно редактирования с исходным изображением, и это отсекает
большинство дешёвых генераторов (docs/research.md §4.3).

Качество приходит параметром из тарифа. Пользователь про него нигде не
спрашивается: это не фича интерфейса, а решение про деньги — разница в цене
между low и medium почти на порядок.
"""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from typing import Any

import httpx

from app.adapters.ai.errors import ProviderResponseError
from app.adapters.ai.http import request_json
from app.adapters.ai.resilience import ResilientCaller
from app.core.models import Photo
from app.ports.ai import ImageQuality


class OpenAIImages:
    """Реализация порта ImageProvider."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        caller: ResilientCaller,
        model: str,
        size: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._caller = caller
        self._model = model
        self._size = size

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def generate(self, prompt: str, *, quality: ImageQuality) -> Photo:
        """Рисует картинку по описанию."""
        payload = {
            "model": self._model,
            "prompt": prompt,
            "size": self._size,
            "quality": quality.value,
            "n": 1,
        }

        async def call() -> dict[str, Any]:
            return await request_json(
                self._client,
                "POST",
                f"{self._base_url}/images/generations",
                headers={**self._auth, "Content-Type": "application/json"},
                json=payload,
            )

        return _to_photo(await self._caller.call(call))

    async def edit(
        self, source: Photo, instruction: str, *, quality: ImageQuality
    ) -> Photo:
        """Переделывает присланное фото по инструкции.

        Отправляется multipart, а не JSON: Images API принимает исходное
        изображение файлом. Заголовок Content-Type не выставляем — httpx сам
        соберёт его вместе с границей раздела частей.
        """
        files = {
            "image": (source.filename, source.data, source.mime_type),
        }
        data = {
            "model": self._model,
            "prompt": instruction,
            "size": self._size,
            "quality": quality.value,
            "n": "1",
        }

        async def call() -> dict[str, Any]:
            return await request_json(
                self._client,
                "POST",
                f"{self._base_url}/images/edits",
                headers=self._auth,
                files=files,
                data=data,
            )

        return _to_photo(await self._caller.call(call))


def _to_photo(payload: dict[str, Any]) -> Photo:
    """Достаёт картинку из ответа.

    Ждём base64 в поле b64_json. Вариант со ссылкой сознательно не
    поддерживаем: ссылка означает второй поход в сеть, ещё один таймаут и ещё
    одну точку отказа ровно там, где пользователь уже ждёт пятнадцать секунд.
    """
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise ProviderResponseError("в ответе провайдера нет картинки")

    first = items[0]
    if not isinstance(first, dict):
        raise ProviderResponseError("картинка в ответе имеет неожиданный вид")

    encoded = first.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise ProviderResponseError("провайдер вернул картинку не в base64")

    try:
        data = b64decode(encoded, validate=True)
    except (Base64Error, ValueError) as error:
        raise ProviderResponseError("картинку не удалось раскодировать") from error

    if not data:
        raise ProviderResponseError("провайдер вернул пустую картинку")

    return Photo(data=data, mime_type="image/png", filename="image.png")
