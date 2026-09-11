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
from app.ports.ai import Answer
from app.ports.observability import Logger


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
        logger: Logger,
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
        self._logger = logger

    async def complete(self, turns: Sequence[ChatTurn], *, model: str) -> Answer:
        """Возвращает ответ на диалог.

        Системный блок стоит первым и не меняется от запроса к запросу —
        именно в таком виде провайдер может переиспользовать посчитанное
        начало промпта. Переменного в нём нет ничего: он приходит из настроек
        целиком, без подстановок про пользователя или время.

        Сколько от этого сэкономлено, видно по ``cached_tokens`` в логе. Ноль
        там означает не поломку порядка, а чаще всего то, что промпт просто
        короче порога кэширования у провайдера: наш системный блок — десятки
        токенов, а не тысячи.
        """
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

        payload_back = await self._caller.call(call)
        self._log_cache(payload_back)
        return _extract_answer(payload_back)

    def _log_cache(self, payload: dict[str, object]) -> None:
        """Пишет в лог, сколько промпта провайдер взял из кэша.

        Без этого числа разговор про кэширование остаётся гаданием: снаружи
        видно только счёт, а по счёту не отличить «кэш работает» от «промпт
        не дотягивает до порога».
        """
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        self._logger.info(
            "llm_usage",
            prompt_tokens=_number(usage.get("prompt_tokens")),
            cached_tokens=_number(cached),
        )


def _role_name(role: Role) -> str:
    return "user" if role is Role.USER else "assistant"


def _number(value: object) -> int:
    """Число из ответа провайдера. Не число — считаем нулём, а не падаем."""
    return value if isinstance(value, int) else 0


def _extract_answer(payload: dict[str, object]) -> Answer:
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

    tokens_in, tokens_out = _tokens(payload)
    # «length» означает, что модель не закончила мысль, а упёрлась в потолок:
    # фраза оборвана на полуслове. Молча отдать такой текст значит выдать
    # огрызок за законченный ответ.
    return Answer(
        text=content.strip(),
        truncated=first.get("finish_reason") == "length",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _tokens(payload: dict[str, object]) -> tuple[int | None, int | None]:
    """Токены запроса и ответа из usage. None — провайдер их не назвал.

    None, а не ноль: не всякий шлюз к /chat/completions возвращает usage, и
    нули в учёте выглядели бы как бесплатные вызовы.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )
