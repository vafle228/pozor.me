"""Production import selection rules, kept conformant with reference/core.py.

Only normalized, already scoped input belongs here. File validation, identity,
manifest verification and transaction ownership remain in importing.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# Match the public reference exception contract for malformed normalized input.
# ruff: noqa: TRY004

def flatten_text(value: object) -> str:
    """Desktop text and text_entities are alternatives, never concatenate both."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Expected text string or rich-text list")
    parts = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            raise ValueError("Malformed rich-text fragment")
    return "".join(parts)


def user_id(from_id: object) -> int | None:
    if isinstance(from_id, str) and re.fullmatch(r"user[1-9][0-9]*", from_id):
        return int(from_id[4:])
    return None


@dataclass(frozen=True)
class Alias:
    username: str
    valid_from: int
    valid_until: int


@dataclass(frozen=True)
class ImportRow:
    message_id: int
    author_id: int | None
    timestamp: int
    text: str
    relation: str


def select_export(
    messages: Iterable[Mapping[str, object]],
    *,
    target_id: int,
    since: int,
    until: int,
    approved_aliases: Sequence[Alias] = (),
    selected_ids: set[int] | None = None,
    neighbors: int = 0,
) -> list[ImportRow]:
    """Select evidence from ONE source chat, already selected by its adapter.

    Aliases must be approved for target and dates before calling. This core
    expects date_unixtime; ISO/timezone fallback belongs to the adapter.
    Caller supplies source namespace and destination scope at persistence.
    It must NOT attach live command side effects to these rows.
    """
    if target_id <= 0 or since >= until or neighbors not in (0, 1):
        raise ValueError("Invalid selection")
    indexed: dict[int, tuple[Mapping[str, object], int, str]] = {}
    for message in messages:
        if message.get("type") != "message":
            continue
        mid = message.get("id")
        if type(mid) is not int or mid <= 0:
            raise ValueError("Invalid message ID")
        if mid in indexed:
            raise ValueError("Duplicate source message ID; resolve in adapter")
        timestamp = message.get("date_unixtime")
        if isinstance(timestamp, bool) or not re.fullmatch(r"[0-9]+", str(timestamp)):
            raise ValueError("Require normalized UTC date_unixtime")
        text = flatten_text(message.get("text", ""))
        indexed[mid] = (message, int(str(timestamp)), text)

    ordered = sorted(indexed, key=lambda mid: (indexed[mid][1], mid))

    def eligible(mid: int) -> bool:
        _, timestamp, text = indexed[mid]
        return (
            since <= timestamp < until
            and bool(text.strip())
            and (selected_ids is None or mid in selected_ids)
        )

    def mentioned(message: Mapping[str, object], timestamp: int) -> bool:
        fragments = message.get("text_entities")
        if fragments is None:
            text = message.get("text", [])
            fragments = text if isinstance(text, list) else []
        if not isinstance(fragments, list):
            raise ValueError("Invalid text_entities")
        aliases = {
            a.username.lstrip("@").casefold()
            for a in approved_aliases
            if a.valid_from <= timestamp < a.valid_until
        }
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            # Desktop serializes this as numeric/string user_id, not from_id.
            if (fragment.get("type") == "mention_name"
                    and str(fragment.get("user_id")) == str(target_id)):
                return True
            if fragment.get("type") == "mention":
                name = str(fragment.get("text", "")).lstrip("@").casefold()
                if name in aliases:
                    return True
        return False

    relations: dict[int, str] = {}
    for mid in ordered:
        if not eligible(mid):
            continue
        message, timestamp, _ = indexed[mid]
        # Forwarded words are not evidence of the sender's own statement.
        if message.get("forwarded_from") or message.get("saved_from"):
            continue
        if user_id(message.get("from_id")) == target_id:
            relations[mid] = "self"
        elif mentioned(message, timestamp):
            relations[mid] = "mention"
        else:
            parent_id = message.get("reply_to_message_id")
            parent = indexed.get(parent_id) if type(parent_id) is int else None
            if parent and user_id(parent[0].get("from_id")) == target_id:
                relations[mid] = "reply"

    if neighbors:
        positions = {mid: index for index, mid in enumerate(ordered)}
        for anchor in list(relations):
            index = positions[anchor]
            message, timestamp, _ = indexed[anchor]
            for near in ordered[max(0, index - 1) : index + 2]:
                other, other_time, _ = indexed[near]
                if (
                    eligible(near)
                    and abs(timestamp - other_time) <= 300
                    and message.get("message_thread_id") == other.get("message_thread_id")
                ):
                    relations.setdefault(near, "context")

    return [
        ImportRow(mid, user_id(indexed[mid][0].get("from_id")),
                  indexed[mid][1], indexed[mid][2], relations[mid])
        for mid in ordered if mid in relations
    ]
