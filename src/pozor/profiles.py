"""Extractive profile v1: exact quotes only, bounded background work, guarded commit.

No semantic inference: repeated text is a quote pattern, never a personality claim.
Cards rank their original evidence; their summaries are not new model evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from pozor.context import normalize
from pozor.inference_queue import InferenceQueue, QueueBusy
from pozor.llm import InferenceError, LlamaClient
from pozor.privacy import blocked
from pozor.storage import connect


def source_rows(
    db: sqlite3.Connection, chat: int, target: int, after: int = 0
) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT m.*,s.attribution,s.relation FROM messages m
        JOIN message_subjects s ON s.message_id=m.id
        WHERE m.scope_chat_id=? AND s.target_id=? AND m.id>?
        AND s.relation IN ('self','mention','reply') AND
        ((m.origin='live' AND julianday(m.occurred_at,
         '+' || (SELECT retention_days FROM chats WHERE chat_id=m.scope_chat_id) || ' days')
         >julianday('now')) OR EXISTS (SELECT 1 FROM import_items ii JOIN imports i
         ON i.batch_id=ii.batch_id WHERE ii.message_id=m.id AND i.target_id=s.target_id
         AND i.chat_id=m.scope_chat_id AND ii.relation=s.relation AND i.status='active'
         AND julianday(i.retained_until)>julianday('now')))
        GROUP BY m.id ORDER BY m.id LIMIT 40""",
        (chat, target, after),
    ).fetchall()


def epoch(db: sqlite3.Connection, chat: int, target: int) -> int:
    row = db.execute(
        "SELECT epoch FROM data_epochs WHERE chat_id=? AND target_id=?", (chat, target)
    ).fetchone()
    return row[0] if row else 0


def commit_batch(
    database: Path,
    chat: int,
    target: int,
    version: int,
    rows: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> bool:
    """Recheck scope, author, entitlement, hash and epoch in the write transaction."""
    if not rows:
        return False
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        if epoch(db, chat, target) != version or blocked(db, chat, target):
            return False
        entitled = {
            r["id"]: r for r in source_rows(db, chat, target, min(r["id"] for r in rows) - 1)
        }
        if any(
            r["id"] not in entitled
            or any(
                r[k] != entitled[r["id"]][k]
                for k in ("text_hash", "author_id", "attribution", "relation", "text")
            )
            for r in rows
        ):
            return False
        groups: dict[tuple[str, str, int | None], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            attr = row["attribution"]
            if attr in {"observed_self", "imported_quote"}:
                if row["author_id"] != target or row["relation"] != "self":
                    return False
            elif attr != "third_party":
                return False
            groups[(normalize(row["text"]), attr, row["author_id"])].append(row)
        if len(groups) > 6:
            return False
        for (text, attr, author), sources in groups.items():
            topic = hashlib.sha256(f"{text}:{attr}:{author}".encode()).hexdigest()[:24]
            # Keep a bounded set of original sources across batches, never resummarize archive.
            old = db.execute(
                "SELECT m.*,s.attribution,s.relation FROM card_evidence ce "
                "JOIN profile_cards c ON c.id=ce.card_id JOIN messages m ON m.id=ce.message_id "
                "JOIN message_subjects s ON s.message_id=m.id AND s.target_id=c.target_id "
                "WHERE c.chat_id=? AND c.target_id=? AND c.topic=?",
                (chat, target, topic),
            ).fetchall()
            combined = {r["id"]: dict(r) for r in [*old, *sources]}
            ordered = sorted(combined.values(), key=lambda r: (r["occurred_at"], r["id"]))
            # Episode = >=30 minutes from the previous episode's last message.
            # A chain of adjacent messages remains one episode, even across batches.
            from datetime import datetime

            selected = ordered[:5]
            # Never report episode counts beyond the retained evidence.
            selected_episodes = 0
            previous = None
            for row in selected:
                stamp = datetime.fromisoformat(row["occurred_at"]).timestamp()
                if previous is None or stamp - previous >= 1800:
                    selected_episodes += 1
                previous = stamp
            kind = (
                ("self_pattern" if selected_episodes >= 3 else "self_quote")
                if attr == "observed_self"
                else ("imported_quote" if attr == "imported_quote" else "third_party_joke")
            )
            old_card = db.execute(
                "SELECT excluded FROM profile_cards WHERE chat_id=? AND target_id=? AND topic=?",
                (chat, target, topic),
            ).fetchone()
            excluded = old_card[0] if old_card else 0
            db.execute(
                "DELETE FROM profile_cards WHERE chat_id=? AND target_id=? AND topic=?",
                (chat, target, topic),
            )
            cursor = db.execute(
                "INSERT INTO profile_cards(chat_id,target_id,topic,summary,attribution,"
                "kind,first_at,last_at,episodes,excluded) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    chat,
                    target,
                    topic,
                    selected[0]["text"],
                    attr,
                    kind,
                    selected[0]["occurred_at"],
                    selected[-1]["occurred_at"],
                    selected_episodes,
                    excluded,
                ),
            )
            for row in selected:
                db.execute(
                    "INSERT INTO card_evidence VALUES (?,?,?)",
                    (cursor.lastrowid, row["id"], row["text_hash"]),
                )
        db.execute(
            "INSERT INTO profile_watermarks VALUES (?,?,?,?) ON CONFLICT(chat_id,target_id) "
            "DO UPDATE SET message_id=excluded.message_id,updated_at=excluded.updated_at",
            (chat, target, max(r["id"] for r in rows), now if now is not None else time.time()),
        )
    return True


def show(database: Path, chat: int, target: int) -> list[dict[str, Any]]:
    from pozor.context import sources_current

    with connect(database) as db:
        if blocked(db, chat, target):
            return []
        result = []
        for row in db.execute(
            "SELECT * FROM profile_cards WHERE chat_id=? AND target_id=? ORDER BY id",
            (chat, target),
        ).fetchall():
            sources = db.execute(
                "SELECT ce.message_id,ce.source_text_hash,m.text_hash,m.origin "
                "FROM card_evidence ce JOIN messages m ON m.id=ce.message_id "
                "WHERE ce.card_id=?",
                (row["id"],),
            ).fetchall()
            if not sources or any(r["source_text_hash"] != r["text_hash"] for r in sources):
                continue
            if not sources_current(
                db, chat, target, [f"message:{r['message_id']}" for r in sources], time.time()
            ):
                continue
            result.append(dict(row) | {"source_ids": [r["message_id"] for r in sources]})
        return result


async def build_once(
    database: Path,
    client: LlamaClient,
    queue: InferenceQueue,
    chat: int,
    target: int,
    *,
    force: bool = False,
) -> bool:
    with connect(database) as db:
        if blocked(db, chat, target):
            return False
        watermark = db.execute(
            "SELECT * FROM profile_watermarks WHERE chat_id=? AND target_id=?", (chat, target)
        ).fetchone()
        rows = source_rows(db, chat, target, watermark["message_id"] if watermark else 0)
        version = epoch(db, chat, target)
    if not rows:
        return False
    from datetime import datetime

    earliest = datetime.fromisoformat(rows[0]["created_at"]).timestamp()
    since = watermark["updated_at"] if watermark else earliest
    if not force and len(rows) < 40 and time.time() - since < 600:
        return False

    async def extract(deadline: float) -> bool:
        batch: list[dict[str, Any]] = []
        groups: set[tuple[str, str, int | None]] = set()
        for row in rows:
            key = (normalize(row["text"]), row["attribution"], row["author_id"])
            if key not in groups and len(groups) == 6:
                break
            candidate = [*batch, dict(row)]
            # Count the entire serialized batch with the actual model tokenizer.
            if await client.tokens(json.dumps(candidate, ensure_ascii=False), deadline) > 1500:
                if not batch:
                    # An oversize source cannot block all later work forever. Advance
                    # only through an explicitly committed skip, with epoch guard.
                    with connect(database) as db:
                        db.execute("BEGIN IMMEDIATE")
                        if epoch(db, chat, target) == version and not blocked(db, chat, target):
                            db.execute(
                                "INSERT INTO profile_watermarks VALUES (?,?,?,?) "
                                "ON CONFLICT(chat_id,target_id) DO UPDATE SET "
                                "message_id=excluded.message_id,updated_at=excluded.updated_at",
                                (chat, target, row["id"], time.time()),
                            )
                break
            groups.add(key)
            batch = candidate
        return commit_batch(database, chat, target, version, batch)

    try:
        return bool(
            await queue.submit(
                key=f"profile:{chat}:{target}",
                chat_id=chat,
                factory=extract,
                background=True,
                deadline=asyncio.get_running_loop().time() + 5,
            )
        )
    except (InferenceError, QueueBusy, TimeoutError):
        return False


async def profile_loop(database: Path, client: LlamaClient, queue: InferenceQueue) -> None:
    while True:
        with connect(database) as db:
            targets = db.execute(
                "SELECT m.chat_id,m.user_id FROM members m JOIN chats c "
                "USING(chat_id) WHERE m.consent=1 AND m.opt_out=0 "
                "AND c.allowed=1 AND c.enabled=1"
            ).fetchall()
        for row in targets:
            try:
                await build_once(database, client, queue, row["chat_id"], row["user_id"])
            except asyncio.CancelledError:
                # Preemption by interactive work cancels a job future, not the scheduler.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
        await asyncio.sleep(30)
