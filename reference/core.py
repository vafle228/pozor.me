"""Pure reference rules. No Telegram, database, network, or LLM dependencies.

Production adapters must validate files, permissions, transactional state,
tokenization, and provenance. All integer times below are UTC Unix seconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Extract a Telegram entity; reject offsets splitting a surrogate pair."""
    raw = text.encode("utf-16-le")
    if offset < 0 or length < 0 or 2 * (offset + length) > len(raw):
        raise ValueError("Entity outside text")
    return raw[2 * offset : 2 * (offset + length)].decode("utf-16-le")


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
    messages: Sequence[Mapping],
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
    indexed: dict[int, tuple[Mapping, int, str]] = {}
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
        indexed[mid] = (message, int(timestamp), text)

    ordered = sorted(indexed, key=lambda mid: (indexed[mid][1], mid))

    def eligible(mid: int) -> bool:
        _, timestamp, text = indexed[mid]
        return (
            since <= timestamp < until
            and bool(text.strip())
            and (selected_ids is None or mid in selected_ids)
        )

    def mentioned(message: Mapping, timestamp: int) -> bool:
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
            if fragment.get("type") == "mention_name":
                # Desktop serializes this as numeric/string user_id, not from_id.
                if str(fragment.get("user_id")) == str(target_id):
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


def reciprocal_rank_fusion(
    rankings: Iterable[tuple[float, Sequence[str]]], k: int = 60
) -> list[tuple[str, float]]:
    """Merge prefiltered lists; duplicate IDs in one list count only once."""
    if k < 1:
        raise ValueError("k must be positive")
    scores: dict[str, float] = {}
    for weight, ranked in rankings:
        if weight < 0:
            raise ValueError("Negative rank weight")
        for rank, eid in enumerate(dict.fromkeys(ranked), start=1):
            scores[eid] = scores.get(eid, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class Evidence:
    id: str
    chat_id: int
    target_id: int
    text: str
    topic: str


def pack_evidence(
    ranked: Sequence[Evidence],
    *,
    chat_id: int,
    target_id: int,
    budget: int,
    token_cost: Callable[[Evidence], int],
    per_topic: int = 2,
) -> list[Evidence]:
    """Greedy HISTORICAL packer; recent reply chain has a separate budget.

    token_cost must include serialized metadata. Validate the complete rendered
    prompt afterward: token counts of separately rendered parts need not add up.
    """
    if budget < 0 or per_topic < 1:
        raise ValueError("Invalid budget")
    result, seen_ids, seen_text = [], set(), set()
    topics: dict[str, int] = {}
    used = 0
    for item in ranked:
        if item.chat_id != chat_id or item.target_id != target_id:
            continue
        normalized = " ".join(item.text.casefold().replace("ё", "е").split())
        if not normalized or item.id in seen_ids or normalized in seen_text:
            continue
        if topics.get(item.topic, 0) >= per_topic:
            continue
        cost = token_cost(item)
        if type(cost) is not int or cost <= 0:
            raise ValueError("token_cost must return a positive integer")
        if used + cost > budget:
            continue
        result.append(item)
        seen_ids.add(item.id)
        seen_text.add(normalized)
        topics[item.topic] = topics.get(item.topic, 0) + 1
        used += cost
    return result


@dataclass(frozen=True)
class Session:
    id: str
    chat_id: int
    thread_id: int
    target_id: int
    initiator_id: int
    revision: int
    evidence_epoch: int
    created_at: int
    last_sent_at: int | None = None
    sent_turns: int = 0
    active: bool = True
    consenting: bool = True


def close_reason(session: Session, now: int) -> str | None:
    if not session.active:
        return "closed"
    if not session.consenting:
        return "opt_out"
    if session.sent_turns >= 4:
        return "turn_limit"
    if now >= session.created_at + 300:
        return "ttl"
    if session.last_sent_at is not None and now >= session.last_sent_at + 90:
        return "silence"
    return None


@dataclass(frozen=True)
class Incoming:
    chat_id: int
    thread_id: int
    actor_id: int
    addresses_session: bool
    stop: bool = False
    finish: bool = False
    admin: bool = False


def decide(session: Session, event: Incoming, now: int) -> str:
    """Return ignore/close:<reason>/defer/continue; parsing/auth are external.

    Defer means coalesce one pending turn until the cooldown permits it,
    then recheck all guards. It does not grant permission to send later.
    """
    if (event.chat_id, event.thread_id) != (session.chat_id, session.thread_id):
        return "ignore"
    reason = close_reason(session, now)
    if reason:
        return "close:" + reason
    authorized = event.actor_id in (session.target_id, session.initiator_id) or event.admin
    if authorized and (event.stop or event.finish):
        return "close:explicit"
    if event.actor_id != session.target_id or not event.addresses_session:
        return "ignore"
    if session.last_sent_at is None:
        return "ignore"
    if now < session.last_sent_at + 10:
        return "defer"
    return "continue"


def may_publish(
    session: Session,
    *,
    job_session_id: str,
    job_revision: int,
    job_epoch: int,
    job_turn: int,
    deadline: int,
    now: int,
) -> bool:
    """Recheck under DB transaction before outbox, and again before send.

    Does not make external HTTP send atomic with a concurrent stop.
    """
    return (
        job_session_id == session.id
        and job_revision == session.revision
        and job_epoch == session.evidence_epoch
        and job_turn == session.sent_turns + 1
        and now < deadline
        and close_reason(session, now) is None
    )
