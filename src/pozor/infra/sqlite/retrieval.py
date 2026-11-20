from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pozor.core.domain.context import ContextPack, Evidence, ProfileMemory
from pozor.core.humor.material import fuse, lexical_relevant, match_query, normalize
from pozor.infra.sqlite.connection import connect


def sources_current(db: sqlite3.Connection, chat: int, target: int,
                    ids: list[str], now: float) -> bool:
    """Check source existence, entitlement and TTL without relying on cleanup timing."""
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    for eid in ids:
        if eid.startswith('trigger:'):
            continue
        if eid.startswith('message:'):
            row = db.execute("SELECT 1 FROM v_entitled_messages WHERE message_id=? "
                             "AND scope_chat_id=? AND target_id=? AND valid_until>julianday(?)",
                             (eid[8:], chat, target, stamp)).fetchone()
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


def retrieve(
    database: Path, *, chat_id: int, thread_id: int, target_id: int,
    trigger_id: int, hint: str, target_label: str, weights: tuple[float, ...] = (1, .8, .5, .6),
    cards_enabled: bool = False, min_terms: int = 2,
) -> ContextPack:
    """Read one SQLite snapshot; cards only rank their original source quotes."""
    if len(weights) != 4:
        raise ValueError("four retrieval weights required")
    with connect(database) as db:
        db.execute("BEGIN")
        chat = db.execute("SELECT * FROM chats WHERE chat_id=? AND allowed=1", (chat_id,)).fetchone()
        if chat is None:
            raise ValueError("chat outside allowlist")
        scope = """ FROM messages m JOIN v_entitled_messages s ON s.message_id=m.id
            WHERE m.scope_chat_id=? AND s.target_id=? AND s.valid_until>julianday('now')
            AND s.relation=(SELECT ss.relation FROM v_entitled_messages ss
                WHERE ss.message_id=m.id AND ss.target_id=s.target_id
                AND ss.valid_until>julianday('now')
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
                    topic="general", policy_version=chat["policy_version"],
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
        query_terms = re.findall(r'"([^"]+)"', query)
        # Synonyms count as one concept, not two independent matches.
        concepts = [{term, {"reddit": "реддит", "реддит": "reddit", "game": "игра", "игра": "game"}.get(term, term)}
                    for term in query_terms]
        concepts = [set(group) for group in {tuple(sorted(group)) for group in concepts}]
        required = 1 if (len(concepts)==1 and len(lexical)==1 and any(len(t)>=5 for t in concepts[0])) else min_terms
        lexical = [eid for eid in lexical if lexical_relevant(rows_by_id[eid].text, concepts,
                    minimum=required)]
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
        card_ids = collect(columns + scope + " AND m.id IN (SELECT ce.message_id FROM profile_item_sources ce "
                           "JOIN profile_items c ON c.id=ce.card_id WHERE c.chat_id=? "
                           "AND c.target_id=? AND (c.provenance IN ('reviewed','automatic') OR ?=1) "
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
            ranked = list(dict.fromkeys([*lexical, *children, *matching_manual, *ranked]))

        current = collect(columns + scope + """ AND m.origin='live' AND coalesce(m.thread_id,0)=?
            AND m.source_message_id!=? ORDER BY m.occurred_at DESC,m.id DESC LIMIT 12""",
            (*params, thread_id, trigger_id))
        recent = [rows_by_id[eid] for eid in reversed(current)]
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
        for card in db.execute("SELECT id,summary FROM profile_items WHERE chat_id=? AND target_id=? "
                               "AND kind='dossier' AND excluded=0 ORDER BY id DESC LIMIT 1", params):
            sources = db.execute("SELECT m.id,m.text_hash,ce.source_text_hash,m.author_id "
                                 "FROM profile_item_sources ce JOIN messages m ON m.id=ce.message_id "
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
