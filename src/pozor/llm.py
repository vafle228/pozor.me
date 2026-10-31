"""llama.cpp HTTP boundary, model tokenizer budgets and bounded validation retry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import Field, ValidationError

from pozor.config import Settings
from pozor.context import ContextPack, Contract, normalize

SYSTEM = Path(__file__).with_name("prompts").joinpath("roast_system.txt").read_text("utf-8")
FALLBACK = "Панч завис на загрузке. Этот раунд пропускаю."


class InferenceError(RuntimeError):
    """Safe error code; never includes a response body, prompt or credential."""


class InvalidOutput(ValueError):
    pass


class GeneratedRoast(Contract):
    text: str = Field(min_length=1, max_length=280)
    evidence_ids: list[str] = Field(max_length=3, json_schema_extra={"uniqueItems": True})
    angle: str = Field(min_length=1, max_length=80)


class GenerationResult(Contract):
    roast: GeneratedRoast
    fallback: bool
    reason: str | None = None
    token_count: int = 0
    context_hash: str = ""


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
        result = await self.request("POST", "tokenize", deadline,
                                    json={"content": text, "add_special": False})
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            raise InferenceError("bad_tokenizer_response")
        return len(result["tokens"])

    async def count(self, messages: list[dict[str, str]], deadline: float) -> int:
        rendered = await self.request("POST", "apply-template", deadline, json={
            "messages": messages, "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        if not isinstance(rendered, dict) or not isinstance(rendered.get("prompt"), str):
            raise InferenceError("bad_template_response")
        return await self.tokens(rendered["prompt"], deadline)

    async def complete(self, messages: list[dict[str, str]], deadline: float) -> str:
        result = await self.request("POST", "v1/chat/completions", deadline, json={
            "model": self.settings.llm_model, "messages": messages, "stream": False,
            "temperature": .7, "top_p": .8, "top_k": 20,
            "max_tokens": self.settings.max_output_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_format": "none",
            "response_format": {"type": "json_object", "schema": GeneratedRoast.model_json_schema()},
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


def messages_for(pack: ContextPack, retry: str | None = None) -> list[dict[str, str]]:
    system = SYSTEM
    if retry:
        system += "\nИсправь ошибку формата: " + retry
    return [{"role": "system", "content": system},
            {"role": "user", "content": pack.model_dump_json()}]


async def pack_context(client: LlamaClient, pack: ContextPack, deadline: float) -> ContextPack:
    """Count serialized metadata, then verify the complete templated prompt."""
    data = pack.model_dump()
    data["recent"], data["evidence"], data["session_turns"] = [], [], []
    seen = {normalize(pack.trigger.text)}
    topics: dict[str, int] = {}
    for name, rows, budget in (
        ("recent", list(reversed(pack.recent)), 900),
        ("evidence", pack.evidence, client.settings.historical_token_budget),
    ):
        used = 0
        for item in rows:
            text = normalize(item.text)
            if not text or text in seen:
                continue
            if name == "evidence" and topics.get(item.topic, 0) >= client.settings.per_topic_limit:
                continue
            cost = await client.tokens(item.model_dump_json(), deadline)
            if used + cost > budget:
                continue
            used += cost
            data[name].append(item.model_dump())
            seen.add(text)
            if name == "evidence":
                topics[item.topic] = topics.get(item.topic, 0) + 1
        if name == "recent":
            data[name].reverse()
    used = 0
    for turn in reversed(pack.session_turns):
        cost = await client.tokens(json.dumps(turn, ensure_ascii=False), deadline)
        if used + cost > 600:
            break
        data["session_turns"].insert(0, turn)
        used += cost
    while True:
        result = ContextPack.model_validate(data)
        count = await client.count(messages_for(result), deadline)
        if count + client.settings.max_output_tokens + 512 <= client.settings.context_size:
            return result
        for name in ("evidence", "session_turns", "recent"):
            if data[name]:
                data[name].pop(-1 if name == "evidence" else 0)
                break
        else:
            raise InferenceError("context_overflow")


def validate_output(raw: str, pack: ContextPack) -> GeneratedRoast:
    try:
        roast = GeneratedRoast.model_validate_json(raw)
    except ValidationError:
        raise InvalidOutput("schema") from None
    if not roast.text.strip() or not roast.angle.strip():
        raise InvalidOutput("empty")
    allowed = {e.evidence_id for e in [pack.trigger, *pack.recent, *pack.evidence]}
    if (not roast.evidence_ids or len(set(roast.evidence_ids)) != len(roast.evidence_ids)
            or not set(roast.evidence_ids) <= allowed):
        raise InvalidOutput("evidence_ids")
    # No author field is accepted. Names and author claims in prose need human evaluation.
    if re.search(r"https?://|www\.|t\.me/|@[\w]+|(?:\+?\d[\s()\-]*){7,}|</?think|"
                 r"[«\"][^»\"]{80,}[»\"]", roast.text + " " + roast.angle, re.IGNORECASE):
        raise InvalidOutput("unsafe_text")
    for old in pack.session_turns:
        a, b = normalize(roast.text).split(), normalize(old).split()
        if a == b:
            raise InvalidOutput("repetition")
        left = set(zip(a, a[1:], a[2:]))
        right = set(zip(b, b[1:], b[2:]))
        if left and right and len(left & right) / len(left | right) > .75:
            raise InvalidOutput("repetition")
    return roast


class Generator:
    def __init__(self, client: LlamaClient):
        self.client = client
        self.failures = 0
        self.open_until = 0.0

    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult:
        try:
            if asyncio.get_running_loop().time() < self.open_until:
                raise InferenceError("circuit_open")
            async with asyncio.timeout_at(deadline):
                packed = await pack_context(self.client, pack, deadline)
                digest = hashlib.sha256(packed.model_dump_json().encode()).hexdigest()
                reason = None
                for attempt in range(2):
                    messages = messages_for(packed, reason)
                    count = await self.client.count(messages, deadline)
                    if count + self.client.settings.max_output_tokens + 512 > self.client.settings.context_size:
                        raise InferenceError("context_overflow")
                    try:
                        raw = await self.client.complete(messages, deadline)
                        roast = validate_output(raw, packed)
                        self.failures = 0
                        return GenerationResult(roast=roast, fallback=False, token_count=count,
                                                context_hash=digest)
                    except InvalidOutput as exc:
                        reason = str(exc)
                        if attempt:
                            raise InferenceError("invalid_output") from None
        except (InferenceError, TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, InferenceError) else "deadline"
            if reason != "circuit_open":
                self.failures += 1
                if self.failures >= 3:
                    self.open_until = asyncio.get_running_loop().time() + 30
            return GenerationResult(
                roast=GeneratedRoast(text=FALLBACK, evidence_ids=[], angle="fallback"),
                fallback=True, reason=reason,
            )
        raise AssertionError("unreachable")
