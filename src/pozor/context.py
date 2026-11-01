"""Scoped direct retrieval and typed, provenance-preserving context contracts."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pozor.storage import connect


def sources_current(db: sqlite3.Connection, chat: int, target: int,
                    ids: list[str], now: float) -> bool:
    """Check source existence, entitlement and TTL without relying on cleanup timing."""
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    for eid in ids:
        if eid.startswith('trigger:'):
            continue
        if eid.startswith('message:'):
            row = db.execute("SELECT 1 FROM messages m JOIN message_subjects s ON s.message_id=m.id "
                             "JOIN chats c ON c.chat_id=m.scope_chat_id WHERE m.id=? "
                             "AND m.scope_chat_id=? AND s.target_id=? AND "
                             "((m.origin='live' AND julianday(m.occurred_at,'+' || c.retention_days || ' days')"
                             ">julianday(?)) OR EXISTS (SELECT 1 FROM import_items ii JOIN imports i "
                             "USING(batch_id) WHERE ii.message_id=m.id AND i.target_id=s.target_id "
                             "AND i.chat_id=m.scope_chat_id AND ii.relation=s.relation AND i.status='active' "
                             "AND julianday(i.retained_until)>julianday(?)))",
                             (eid[8:], chat, target, stamp, stamp)).fetchone()
        elif eid.startswith('manual:'):
            row = db.execute("SELECT 1 FROM manual_items mi JOIN imports i USING(batch_id) "
                             "WHERE mi.item_id=? AND mi.target_id=? AND i.chat_id=? AND i.target_id=? "
                             "AND i.status='active' AND julianday(i.retained_until)>julianday(?)",
                             (eid[7:], target, chat, target, stamp)).fetchone()
        else:
            return False
        if row is None:
            return False
    return True


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


def normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def match_query(text: str) -> str:
    # Version 1 dictionary. Raw MATCH syntax is never passed to SQLite.
    synonyms = {"reddit": "реддит", "реддит": "reddit", "game": "игра", "игра": "game"}
    terms: list[str] = []
    stop = {'что', 'как', 'кто', 'где', 'когда', 'почему', 'зачем', 'о', 'об', 'в', 'на', 'из', 'за', 'к', 'по', 'и', 'а', 'но', 'у', 'он', 'она', 'его', 'ее', 'мне', 'меня', 'ты', 'тебе', 'тебя', 'это', 'том', 'то', 'же', 'бы', 'ли', 'ну', 'бот', 'прожарь', 'прожарка', 'думаешь', 'думаете', 'скажи', 'расскажи', 'участник', 'пожалуйста'}
    for word in normalize(text).split():
        if word in stop:
            continue
        for term in (word, synonyms.get(word, word)):
            if term not in terms and len(terms) < 12:
                terms.append(term)
    return " OR ".join('"' + term + '"' + ('*' if re.fullmatch(r'[а-я]{5,}', term) else '') for term in terms)


def fuse(rankings: Sequence[tuple[float, Sequence[str]]], k: int = 60) -> list[str]:
    if k < 1 or any(not math.isfinite(weight) or weight < 0 for weight, _ in rankings):
        raise ValueError("invalid RRF parameters")
    scores: dict[str, float] = {}
    for weight, ids in rankings:
        for rank, eid in enumerate(dict.fromkeys(ids), 1):
            scores[eid] = scores.get(eid, 0) + weight / (k + rank)
    return sorted(scores, key=lambda eid: (-scores[eid], eid))


def retrieve(
    database: Path, *, chat_id: int, thread_id: int, target_id: int,
    trigger_id: int, hint: str, target_label: str, weights: tuple[float, ...] = (1, .8, .5, .6),
    cards_enabled: bool = False,
) -> ContextPack:
    """Read one SQLite snapshot; cards only rank their original source quotes."""
    if len(weights) != 4:
        raise ValueError("four retrieval weights required")
    with connect(database) as db:
        db.execute("BEGIN")
        chat = db.execute("SELECT * FROM chats WHERE chat_id=? AND allowed=1", (chat_id,)).fetchone()
        if chat is None:
            raise ValueError("chat outside allowlist")
        scope = """ FROM messages m JOIN message_subjects s ON s.message_id=m.id
            WHERE m.scope_chat_id=? AND s.target_id=? AND
            ((m.origin='live' AND julianday(m.occurred_at,
                '+' || (SELECT retention_days FROM chats WHERE chat_id=m.scope_chat_id) || ' days')
                >julianday('now')) OR EXISTS (
                SELECT 1 FROM import_items ii JOIN imports i ON i.batch_id=ii.batch_id
                WHERE ii.message_id=m.id AND i.chat_id=m.scope_chat_id
                AND i.target_id=s.target_id AND ii.relation=s.relation AND i.status='active'
                AND julianday(i.retained_until)>julianday('now')))
            AND s.relation=(SELECT ss.relation FROM message_subjects ss
                WHERE ss.message_id=m.id AND ss.target_id=s.target_id
                ORDER BY CASE ss.relation WHEN 'self' THEN 0 WHEN 'reply' THEN 1
                    WHEN 'mention' THEN 2 ELSE 3 END LIMIT 1) """
        params = (chat_id, target_id)
        rows_by_id: dict[str, Evidence] = {}

        def collect(sql: str, args: tuple[object, ...]) -> list[str]:
            ids = []
            for row in db.execute(sql, args).fetchall():
                eid = f"message:{row['id']}"
                ids.append(eid)
                if eid in rows_by_id:
                    continue
                rows_by_id[eid] = Evidence(
                    evidence_id=eid, chat_id=chat_id, allowed_target_id=target_id,
                    source_namespace=row["source_namespace"],
                    source_message_id=row["source_message_id"], author_id=row["author_id"],
                    thread_id=row["thread_id"],
                    relation=row["relation"], attribution=row["attribution"], text=row["text"],
                    timestamp=row["occurred_at"], text_hash=row["text_hash"],
                    topic=f"thread:{row['thread_id'] or 0}", policy_version=chat["policy_version"],
                    reply_source_id=row["reply_source_id"],
                    parent_status="missing" if row["reply_source_id"] else "none",
                )
            return ids

        columns = "SELECT m.*,s.relation,s.attribution"
        label_words = set(normalize(target_label).split())
        query = match_query(' '.join(w for w in normalize(hint).split() if w not in label_words))
        lexical = collect(columns + scope + """ AND m.id IN
            (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)
            ORDER BY (SELECT rank FROM messages_fts WHERE rowid=m.id
                      AND messages_fts MATCH ?) ASC, m.id LIMIT 40""",
            (*params, query, query)) if query else []
        recent_self = collect(columns + scope +
            " AND s.relation='self' ORDER BY m.occurred_at DESC,m.id DESC LIMIT 40", params)
        mentions = collect(columns + scope +
            " AND s.relation IN ('mention','reply') ORDER BY m.occurred_at DESC,m.id DESC LIMIT 40",
            params)
        manual_ids = []
        for row in db.execute("""SELECT mi.* FROM manual_items mi JOIN imports i
            ON i.batch_id=mi.batch_id WHERE i.chat_id=? AND mi.target_id=?
            AND i.target_id=? AND i.status='active' AND julianday(i.retained_until)>julianday('now')
            ORDER BY i.created_at DESC,mi.item_id LIMIT 40""",
            (chat_id, target_id, target_id)):
            eid = f"manual:{row['item_id']}"
            manual_ids.append(eid)
            rows_by_id[eid] = Evidence(
                evidence_id=eid, chat_id=chat_id, allowed_target_id=target_id,
                source_namespace=f"manual:{row['batch_id']}", relation="manual",
                attribution="manual_claim", text=row["text"], timestamp=row["occurred_at"],
                text_hash=row["text_hash"], topic="dossier", policy_version=chat["policy_version"],
            )
        card_ids = collect(columns + scope + " AND m.id IN (SELECT ce.message_id FROM card_evidence ce "
                           "JOIN profile_cards c ON c.id=ce.card_id WHERE c.chat_id=? "
                           "AND c.target_id=? AND (c.profile_version IN (2,3) OR ?=1) "
                           "AND c.excluded=0 AND ce.source_text_hash=m.text_hash) "
                           "ORDER BY m.id DESC LIMIT 40", (*params, *params, int(cards_enabled)))
        ranked = fuse(list(zip(weights, [lexical, recent_self, mentions, [*manual_ids, *card_ids]], strict=True)))
        # The current subject outranks general recency, even when it only occurs
        # in one older quote. Recent messages must not consume its entire budget.
        if lexical:
            matched_parent_ids = tuple(int(eid[8:]) for eid in lexical)
            placeholders = ','.join('?' for _ in matched_parent_ids)
            children = collect(columns + scope + ' AND EXISTS (SELECT 1 FROM messages p '
                'WHERE p.source_namespace=m.source_namespace AND p.source_message_id=m.reply_source_id '
                f'AND p.id IN ({placeholders})) ORDER BY m.id DESC LIMIT 20', (*params, *matched_parent_ids))
            query_terms = re.findall(r'"([^"]+)"', query)
            matching_manual = [eid for eid in manual_ids if any(
                term in normalize(rows_by_id[eid].text) for term in query_terms)]
            ranked = list(dict.fromkeys([*lexical, *children, *matching_manual]))

        current = collect(columns + scope + """ AND m.origin='live' AND coalesce(m.thread_id,0)=?
            AND m.source_message_id!=? ORDER BY m.occurred_at DESC,m.id DESC LIMIT 20""",
            (*params, thread_id, trigger_id))
        recent = [rows_by_id[eid] for eid in reversed(current)]
        relevant = set(lexical)
        recent = [item for item in recent if item.evidence_id not in relevant]
        if lexical:
            recent = []
        history = [rows_by_id[eid] for eid in ranked if eid not in {e.evidence_id for e in recent}
                   and not (rows_by_id[eid].source_namespace == f"live:{chat_id}"
                            and rows_by_id[eid].source_message_id == trigger_id)]
        # A parent must itself be entitled to this target; namespace alone is insufficient.
        for child in [*recent, *history]:
            if child.reply_source_id is None:
                continue
            parent_ids = collect(columns + scope +
                " AND m.source_namespace=? AND m.source_message_id=? LIMIT 1",
                (*params, child.source_namespace, child.reply_source_id))
            if parent_ids:
                updated = child.model_copy(update={"parent_status": "available"})
                rows_by_id[child.evidence_id] = updated
                parent = rows_by_id[parent_ids[0]]
                if (parent.evidence_id not in {e.evidence_id for e in [*recent, *history]}
                        and not (parent.source_namespace == f"live:{chat_id}"
                                 and parent.source_message_id == trigger_id)):
                    history.append(parent)
        epoch = db.execute("SELECT epoch FROM data_epochs WHERE chat_id=? AND target_id=?",
                           params).fetchone()
        dossier = []
        for card in db.execute("SELECT id,summary FROM profile_cards WHERE chat_id=? AND target_id=? "
                               "AND profile_version=4 AND excluded=0 ORDER BY id DESC LIMIT 1", params):
            sources = db.execute("SELECT m.id,m.text_hash,ce.source_text_hash,m.author_id "
                                 "FROM card_evidence ce JOIN messages m ON m.id=ce.message_id "
                                 "WHERE ce.card_id=?", (card['id'],)).fetchall()
            ids = [f"message:{r['id']}" for r in sources]
            if (ids and all(r['author_id'] == target_id and r['text_hash'] == r['source_text_hash']
                            for r in sources) and sources_current(db, chat_id, target_id, ids,
                                                                  datetime.now(UTC).timestamp())):
                dossier.append(ProfileMemory(summary=card['summary'], evidence_ids=ids))
        member = db.execute("SELECT current_username,display_name FROM members WHERE chat_id=? AND user_id=?",
                            (chat_id, target_id)).fetchone()
        target_username = member[0] if member else None
    return ContextPack(
        chat_id=chat_id, thread_id=thread_id, target_id=target_id, target_label=target_label,
        target_username=target_username, dossier=dossier,
        trigger=Evidence(evidence_id=f"trigger:{trigger_id}", chat_id=chat_id,
                         allowed_target_id=target_id, source_namespace="request",
                         source_message_id=trigger_id, relation="trigger",
                         attribution="caller_hint", text=hint),
        recent=[rows_by_id[e.evidence_id] for e in recent],
        evidence=[rows_by_id[e.evidence_id] for e in history],
        evidence_epoch=epoch[0] if epoch else 0,
    )
