"""Routing decisions for the bounded v0.2 round."""

import re
from dataclasses import dataclass
from typing import Literal

from pozor.core.commands.parsing import ParsedCommand, roast_hint


@dataclass(frozen=True)
class ActiveRound:
    session_id: str
    target_id: int
    initiator_id: int
    trigger_id: int
    target_label: str
    mode: str
    direct_reply: bool

    def action(
        self, message_id: int, actor_id: int, text: str, mention: str | None
    ) -> Literal["first", "continue", "close", "ignore"]:
        if message_id == self.trigger_id:
            return "first"
        if not (self.direct_reply or mention) or actor_id not in (
            self.target_id,
            self.initiator_id,
        ):
            return "ignore"
        phrase = text.casefold().strip().strip(".! ,")
        if mention:
            phrase = phrase.replace("@" + mention.casefold(), "").strip().strip(".! ,")
        if actor_id == self.target_id and phrase in {"стоп", "хватит", "отстань", "не продолжай"}:
            return "close"
        if self.direct_reply and actor_id == self.initiator_id and phrase == "всё, доминация":
            return "close"
        return "ignore" if text.startswith("/") else "continue"

    def hint(self, text: str, command: ParsedCommand | None, quote: str | None, first: bool) -> str:
        hint = roast_hint(command, self.target_label) if command else text
        if quote:
            hint += "\nРеплика участника: " + quote
        if hint.strip():
            return hint.strip()
        if first:
            return "Привет!" if self.mode == "dialogue" else "Дружеская прожарка участника."
        return "Продолжи по той же теме."


def may_continue_mention(command: ParsedCommand, text: str) -> bool:
    """Slash commands and an explicit self-roast start a separate round."""
    return text.startswith("@") and not re.match(
        r"^(?:меня|себя|me)(?=\s|$)", command.argument, re.IGNORECASE
    )
