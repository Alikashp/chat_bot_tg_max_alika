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
from collections.abc import Sequence
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
        edit_size: str = "auto",
        input_fidelity: str = "high",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._caller = caller
        self._model = model
        self._size = size
        self._edit_size = edit_size
        self._input_fidelity = input_fidelity

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
        self, sources: Sequence[Photo], instruction: str, *, quality: ImageQuality
    ) -> Photo:
        """Переделывает присланные фото по инструкции.

        Отправляется multipart, а не JSON: Images API принимает исходные
        изображения файлами. Заголовок Content-Type не выставляем — httpx сам
        соберёт его вместе с границей раздела частей.

        Два параметра отличают правку от рисования заново, и оба здесь
        существенны — без них человек получает не своё лицо.

        ``input_fidelity`` просит модель сохранять черты лица и мелкие детали
        исходника. По умолчанию Images API перерисовывает картинку целиком, и
        никакая инструкция «оставь человека как есть» этого не отменяет: это
        не вопрос формулировки, а вопрос параметра.

        ``size`` для правки — «auto», то есть пропорции исходника. Фиксированный
        квадрат означал бы, что портретный снимок с телефона модель
        перекомпонует под 1:1, а вместе с кадром переедет и лицо.

        Порядок ``sources`` уезжает провайдеру как есть: инструкция ссылается
        на снимки по номерам, а детали первого он вытягивает сильнее прочих.
        """
        if not sources:
            raise ValueError("нужно хотя бы одно исходное фото")

        files = _files(sources)
        data = {
            "model": self._model,
            "prompt": instruction,
            "size": self._edit_size,
            "quality": quality.value,
            "n": "1",
        }
        if self._input_fidelity:
            # Пустое значение выключает параметр целиком: не всякий шлюз к
            # Images API его пропускает, а неизвестное поле — это 400 на
            # каждый прикол.
            data["input_fidelity"] = self._input_fidelity

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


def _files(sources: Sequence[Photo]) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Части multipart с исходными фото.

    Одно фото уходит полем ``image``, несколько — полем ``image[]``. Images API
    принимает массив и под коротким именем, но одиночный запрос — это все
    приколы, кроме одного, и путь у них остаётся ровно тот, что уже работает
    в бою. Менять его ради единообразия значило бы проверять на пользователях
    то, что и так проверено.

    Имена частей при этом нумеруются. Из MAX оба снимка приезжают под одним и
    тем же ``photo.jpg``, а между нами и Images API стоит шлюз, про разбор
    внутри которого мы ничего не знаем. Одинаковые имена — единственное, чем
    он мог бы склеить две части в одну, и человек получил бы полароид, на
    котором он дважды взрослый.
    """
    if len(sources) == 1:
        first = sources[0]
        return [("image", (first.filename, first.data, first.mime_type))]

    return [
        ("image[]", (f"{number}-{source.filename}", source.data, source.mime_type))
        for number, source in enumerate(sources, start=1)
    ]


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
