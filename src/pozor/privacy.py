"""Transactional group-scoped forgetting and bounded retention; no disk-erasure promise."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pozor.storage import connect


def blocked(db: sqlite3.Connection, chat: int, user: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM forgotten_members WHERE chat_id=? AND user_id=?", (chat, user)
        ).fetchone()
        is not None
    )


def bump(db: sqlite3.Connection, chat: int, target: int) -> None:
    db.execute(
        "INSERT INTO data_epochs VALUES (?,?,1) ON CONFLICT(chat_id,target_id) "
        "DO UPDATE SET epoch=epoch+1",
        (chat, target),
    )


def purge_sessions(db: sqlite3.Connection, chat: int) -> None:
    # Any session in this group may contain a quote about the removed person.
    db.execute(
        "UPDATE sessions SET state='closed',closed_reason='purge',revision=revision+1 "
        "WHERE chat_id=?",
        (chat,),
    )
    db.execute(
        "DELETE FROM turns WHERE session_id IN (SELECT id FROM sessions WHERE chat_id=?)", (chat,)
    )


def forget(database: Path, chat: int, target: int) -> None:
    """Remove authored/subject-linked text, including shared messages in this group."""
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            db.execute(
                "SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat, target)
            ).fetchone()
            is None
        ):
            raise ValueError("unknown group member")
        db.execute(
            "INSERT OR IGNORE INTO forgotten_members(chat_id,user_id) VALUES (?,?)", (chat, target)
        )
        db.execute(
            "UPDATE members SET consent=0,opt_out=1,display_name=NULL,current_username=NULL,"
            "last_seen=NULL,consent_version=consent_version+1 WHERE chat_id=? AND user_id=?",
            (chat, target),
        )
        db.execute("DELETE FROM aliases WHERE chat_id=? AND user_id=?", (chat, target))
        affected = {target}
        rows = db.execute(
            "SELECT DISTINCT m.id,s.target_id FROM messages m LEFT JOIN "
            "message_subjects s ON s.message_id=m.id WHERE m.scope_chat_id=? AND "
            "(m.author_id=? OR EXISTS (SELECT 1 FROM message_subjects x "
            "WHERE x.message_id=m.id AND x.target_id=?))",
            (chat, target, target),
        ).fetchall()
        for row in rows:
            if row["target_id"] is not None:
                affected.add(row["target_id"])
        for mid in {r["id"] for r in rows}:
            db.execute("DELETE FROM messages WHERE id=?", (mid,))
        db.execute(
            "DELETE FROM manual_items WHERE batch_id IN "
            "(SELECT batch_id FROM imports WHERE chat_id=? AND target_id=?)",
            (chat, target),
        )
        # Invalidate every preview in this group: it may include shared deleted text.
        db.execute(
            "UPDATE imports SET status='expired' WHERE chat_id=? AND status='staged'", (chat,)
        )
        db.execute(
            "DELETE FROM import_items WHERE batch_id IN "
            "(SELECT batch_id FROM imports WHERE chat_id=? AND target_id=?)",
            (chat, target),
        )
        db.execute(
            "UPDATE imports SET status='expired',source_file='',source_hash='' "
            "WHERE chat_id=? AND target_id=?",
            (chat, target),
        )
        purge_sessions(db, chat)
        db.execute("DELETE FROM command_receipts WHERE chat_id=?", (chat,))
        # Raw normalized control/reply payloads can contain nested target quotes. Drop
        # the entire group's inbox, preserving the independently committed poller offset.
        for row in db.execute("SELECT update_id,normalized_payload FROM inbox").fetchall():
            try:
                payload = json.loads(row["normalized_payload"]) if row["normalized_payload"] else {}
            except json.JSONDecodeError:
                db.execute("DELETE FROM inbox WHERE update_id=?", (row["update_id"],))
                continue
            if _contains_chat(payload, chat):
                db.execute("DELETE FROM inbox WHERE update_id=?", (row["update_id"],))
        for user in affected:
            bump(db, chat, user)
    remove_staging(database, chat=chat)


def _contains_chat(value: object, chat: int) -> bool:
    if isinstance(value, dict):
        nested = value.get("chat")
        if isinstance(nested, dict) and nested.get("id") == chat:
            return True
        return any(_contains_chat(item, chat) for item in value.values())
    return isinstance(value, list) and any(_contains_chat(item, chat) for item in value)


def remove_staging(database: Path, *, chat: int | None = None) -> int:
    """Only remove application-owned staging files, never arbitrary operator paths.

    Custom preview paths are made unusable in SQL, but remain operator-owned files.
    """
    root = (database.parent / "staging").resolve()
    removed = 0
    with connect(database) as db:
        paths = db.execute(
            "SELECT manifest_path FROM imports WHERE status IN "
            "('expired','rolled_back') OR chat_id=?",
            (chat,),
        ).fetchall()
    for row in paths:
        path = Path(row[0]).resolve()
        if path.is_relative_to(root) and path.is_file():
            path.unlink()
            removed += 1
    return removed


def cleanup(database: Path, *, now: datetime | None = None, import_days: int = 90) -> None:
    from pozor.importing import rollback_import
    from pozor.ingestion import cleanup_expired_live_data

    current = now or datetime.now(UTC)
    if current.tzinfo is None or import_days < 1:
        raise ValueError("invalid retention arguments")
    with connect(database) as db:
        batches = db.execute(
            "SELECT batch_id FROM imports WHERE status='active' AND "
            "julianday(coalesce(retained_until,datetime(created_at,?)))<=julianday(?)",
            (f"+{import_days} days", current.isoformat()),
        ).fetchall()
    for row in batches:
        rollback_import(database, row["batch_id"])
    cleanup_expired_live_data(database, now=current)
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE imports SET status='expired' WHERE status='staged' "
            "AND julianday(expires_at)<=julianday(?)",
            (current.isoformat(),),
        )
        # Purge session text if old or any source changed. Epoch invalidation guards
        # pending jobs; removing inputs/outbox prevents later reuse or replay.
        for row in db.execute(
            "SELECT DISTINCT s.chat_id FROM sessions s LEFT JOIN turns t "
            "ON t.session_id=s.id LEFT JOIN data_epochs e ON e.chat_id=s.chat_id "
            "AND e.target_id=s.target_id WHERE s.created_at<? OR "
            "t.evidence_epoch<>coalesce(e.epoch,0)",
            (int((current - timedelta(days=1)).timestamp()),),
        ).fetchall():
            purge_sessions(db, row["chat_id"])
        db.execute(
            "DELETE FROM inbox WHERE julianday(received_at)<julianday(?)",
            ((current - timedelta(days=1)).isoformat(),),
        )
    remove_staging(database)
