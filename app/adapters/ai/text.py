"""Провайдер текстовых ответов через OpenAI-совместимый API.

Свой тонкий клиент на httpx, а не SDK. Причины разобраны в docs/research.md
§4.1 и сводятся к двум: respx из задания мокает именно транспорт httpx, а
собственные повторы SDK конфликтовали бы с нашими из infra/retry.py —два слоя
повторов множат нагрузку на лежащий провайдер.

Формат /chat/completions — де-факто стандарт: на него отвечают и OpenAI, и
OpenRouter, и десяток других шлюзов. Сменить провайдера — сменить base_url и
ключ в переменных окружения, не трогая код.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.adapters.ai.errors import ProviderResponseError
from app.adapters.ai.http import request_json
from app.adapters.ai.resilience import ResilientCaller
from app.core.models import ChatTurn, Role


class OpenAICompatibleLLM:
    """Реализация порта LLMProvider."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        caller: ResilientCaller,
        system_prompt: str,
        max_tokens: int,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._caller = caller
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens

    async def complete(self, turns: Sequence[ChatTurn], *, model: str) -> str:
        """Возвращает ответ на диалог."""
        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        messages.extend(
            {"role": _role_name(turn.role), "content": turn.content} for turn in turns
        )

        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": self._max_tokens,
        }

        async def call() -> dict[str, object]:
            return await request_json(
                self._client,
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers,
                json=payload,
            )

        return _extract_answer(await self._caller.call(call))


def _role_name(role: Role) -> str:
    return "user" if role is Role.USER else "assistant"


def _extract_answer(payload: dict[str, object]) -> str:
    """Достаёт текст ответа.

    Пустой ответ считаем сбоем, а не ответом: показать пользователю пустое
    сообщение хуже, чем честную ошибку с кнопкой «Повторить» — тем более что
    лимит за ошибку не спишется.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError("в ответе провайдера нет вариантов ответа")

    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderResponseError("вариант ответа имеет неожиданный вид")

    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError("в варианте ответа нет сообщения")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderResponseError("провайдер вернул пустой ответ")

    return content.strip()
