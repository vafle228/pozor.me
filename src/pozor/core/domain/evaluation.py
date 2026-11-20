from typing import Literal

from pydantic import Field

from pozor.core.domain.context import Contract


class EvalQuote(Contract):
    text: str
    author: int = 42
    target: int = 42
    chat: int = -1
    relation: Literal["self", "mention", "reply", "context"] = "self"


class EvalCase(Contract):
    id: str
    category: Literal["callback", "empty", "identity", "adversarial", "control"]
    hint: str
    quotes: list[EvalQuote] = Field(default_factory=list, max_length=40)
    expected: list[int] = Field(default_factory=list)
    action: Literal["none", "stop", "edit", "forget", "deadline", "optout"] = "none"


