from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Evidence(Contract):
    evidence_id: str
    chat_id: int
    allowed_target_id: int
    source_namespace: str
    source_message_id: int | None = None
    thread_id: int | None = None
    author_id: int | None = None
    relation: Literal["self", "mention", "reply", "context", "manual", "trigger"]
    attribution: Literal[
        "observed_self", "third_party", "manual_claim", "imported_quote", "caller_hint"
    ]
    text: str
    timestamp: str | None = None
    text_hash: str = ""
    topic: str = "general"
    policy_version: int = 1
    reply_source_id: int | None = None
    parent_status: Literal["none", "available", "missing"] = "none"

    @model_validator(mode="after")
    def check_author(self) -> Evidence:
        if self.attribution in {"observed_self", "imported_quote"} and (
            self.author_id != self.allowed_target_id or self.relation != "self"
        ):
            raise ValueError("self evidence requires matching author and subject")
        return self


class ConversationTurn(Contract):
    role: Literal["user", "assistant"]
    author_id: int | None = None
    text: str


class ProfileMemory(Contract):
    summary: str
    evidence_ids: list[str]


class Hook(Contract):
    quote: str = Field(min_length=1, max_length=120)
    why_funny: str = Field(min_length=1, max_length=160)
    type: Literal["obsession", "contradiction", "brag", "fail", "catchphrase", "absurd_detail"]
    heat: int = Field(ge=1, le=3)
    source_id: int = Field(gt=0)


class ContextPack(Contract):
    mode: Literal["roast", "dialogue"] = "roast"
    chat_id: int
    thread_id: int
    target_id: int
    target_label: str
    target_username: str | None = None
    initiator_id: int | None = None
    speaker_id: int | None = None
    trigger: Evidence
    recent: list[Evidence] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    session_turns: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(default_factory=list)
    dossier: list[ProfileMemory] = Field(default_factory=list)
    forbidden_angles: list[str] = Field(default_factory=list)
    level: int = Field(default=2, ge=1, le=3)
    hooks: list[Hook] = Field(default_factory=list)
    few_shot: list[ConversationTurn] = Field(default_factory=list)
    repetition_texts: list[str] = Field(default_factory=list)
    recent_mechanisms: list[str] = Field(default_factory=list)
    evidence_epoch: int = 0

    @model_validator(mode="after")
    def check_scope(self) -> ContextPack:
        rows = [self.trigger, *self.recent, *self.evidence]
        if any((e.chat_id, e.allowed_target_id) != (self.chat_id, self.target_id) for e in rows):
            raise ValueError("foreign context scope")
        if len({e.evidence_id for e in rows}) != len(rows):
            raise ValueError("duplicate evidence IDs")
        if self.trigger.attribution != "caller_hint":
            raise ValueError("trigger must remain caller_hint")
        if any((e.thread_id or 0) != self.thread_id or not e.source_namespace.startswith("live:")
               for e in self.recent):
            raise ValueError("recent context must be live and in the current thread")
        return self


