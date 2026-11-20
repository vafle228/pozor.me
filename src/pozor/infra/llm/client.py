from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Self

import httpx

from pozor.config import Settings
from pozor.core.domain.generation import GeneratedRoast, InferenceError, InvalidOutput


class LlamaClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        headers = {}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key.get_secret_value()}"
        self.http = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/", headers=headers,
            transport=transport, trust_env=False, follow_redirects=False,
        )
        self._token_cache: dict[str, int] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, deadline: float, **kwargs: Any) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise InferenceError("deadline")
        try:
            async with asyncio.timeout(remaining):
                response = await self.http.request(
                    method, path, timeout=httpx.Timeout(remaining, connect=min(1, remaining)), **kwargs
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise InferenceError("llama_http_or_json") from None

    async def check(self, deadline: float) -> None:
        health = await self.request("GET", "health", deadline)
        models = await self.request("GET", "v1/models", deadline)
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise InferenceError("not_ready")
        if (not isinstance(models, dict) or not isinstance(models.get("data"), list) or not any(
            isinstance(m, dict) and m.get("id") == self.settings.llm_model
            for m in models.get("data", [])
        )):
            raise InferenceError("model_mismatch")
        slots = await self.request("GET", "slots", deadline)
        if (not isinstance(slots, list) or len(slots) != 1 or not isinstance(slots[0], dict)
                or type(slots[0].get("n_ctx")) is not int
                or slots[0]["n_ctx"] < self.settings.context_size):
            raise InferenceError("slot_context_mismatch")

    async def tokens(self, text: str, deadline: float) -> int:
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self._token_cache:
            return self._token_cache[key]
        result = await self.request("POST", "tokenize", deadline,
                                    json={"content": text, "add_special": False})
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            raise InferenceError("bad_tokenizer_response")
        count = len(result['tokens'])
        if len(self._token_cache) >= 512:
            self._token_cache.clear()
        self._token_cache[key] = count
        return count

    async def count(self, messages: list[dict[str, str]], deadline: float) -> int:
        rendered = await self.request("POST", "apply-template", deadline, json={
            "messages": messages, "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        if not isinstance(rendered, dict) or not isinstance(rendered.get("prompt"), str):
            raise InferenceError("bad_template_response")
        return await self.tokens(rendered["prompt"], deadline)

    async def complete(self, messages: list[dict[str, str]], deadline: float) -> str:
        return await self.complete_json(messages, deadline, schema=GeneratedRoast.model_json_schema())

    async def complete_json(self, messages: list[dict[str, str]], deadline: float, *,
                            schema: dict[str, Any], temperature: float | None = None,
                            max_tokens: int | None = None) -> str:
        result = await self.request("POST", "v1/chat/completions", deadline, json={
            "model": self.settings.llm_model, "messages": messages, "stream": False,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "top_p": self.settings.top_p, "min_p": self.settings.min_p,
            "presence_penalty": self.settings.presence_penalty,
            "max_tokens": self.settings.max_output_tokens if max_tokens is None else max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}, "reasoning_format": "auto",
            "response_format": {"type": "json_object", "schema": schema},
        })
        try:
            choice = result["choices"][0]
            message = choice["message"]
            content = message["content"]
            if message.get("reasoning_content") or message.get("tool_calls"):
                raise InvalidOutput("thinking_or_tools")
            usage = result.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            if details.get("reasoning_tokens", 0):
                raise InvalidOutput("thinking_tokens")
            if choice.get("finish_reason") != "stop" or not isinstance(content, str):
                raise InvalidOutput("incomplete")
            return content
        except (KeyError, IndexError, TypeError, AttributeError):
            raise InvalidOutput("malformed_completion") from None

    async def wait_idle(self, deadline: float) -> None:
        # Closing the HTTP connection does not prove that the GPU slot was released.
        while True:
            slots = await self.request("GET", "slots", deadline)
            if not isinstance(slots, list) or len(slots) != 1:
                raise InferenceError("expected_one_slot")
            if not isinstance(slots[0], dict):
                raise InferenceError("bad_slot_response")
            if slots[0].get("is_processing") is False:
                return
            await asyncio.sleep(min(.025, max(0, deadline - asyncio.get_running_loop().time())))


