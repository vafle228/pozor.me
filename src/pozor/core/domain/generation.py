from __future__ import annotations

from pydantic import Field

from pozor.core.domain.context import Contract


class InferenceError(RuntimeError):
    """Safe error code; never includes a response body, prompt or credential."""


class InvalidOutput(ValueError):
    def __init__(self, reason: str, *, field: str | None = None, rule: str | None = None):
        super().__init__(reason)
        self.field = field
        self.rule = rule


class GeneratedRoast(Contract):
    angle: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=280)
    evidence_ids: list[str] = Field(max_length=8, json_schema_extra={"uniqueItems": True})


class GenerationResult(Contract):
    roast: GeneratedRoast
    fallback: bool
    reason: str | None = None
    token_count: int = 0
    context_hash: str = ""
    candidates: list[dict[str, str]] = Field(default_factory=list)
    judge_reason: str = ""
    latency_ms: float = 0
    prompt_version: str = "v1"
    generation_id: int | None = None


