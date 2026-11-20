"""Durable recovery and source checks for legacy turns."""

import json
import sqlite3
from pathlib import Path

from pozor.context import sources_current
from pozor.infra.sqlite.commands import close_sessions_on_startup
from pozor.infra.sqlite.connection import connect


def recover_delivery(database: Path) -> None:
    """Crash after possible delivery must not cause a blind resend."""
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE outbox SET status='unknown',error_code='restart' WHERE status='sending'")
        db.execute("UPDATE turns SET status='unknown' WHERE status='sending'")
        db.execute("UPDATE outbox SET status='cancelled' WHERE status='ready'")
        db.execute(
            "UPDATE turns SET status='cancelled' WHERE status IN ('pending','queued','generating','ready')"
        )
    close_sessions_on_startup(database)


def guard(db: sqlite3.Connection, turn: sqlite3.Row, now: float) -> bool:
    """Production equivalent of may_publish, plus chat and durable consent guards."""
    if (
        turn["status"] in {"ready", "sending"}
        and turn["context_hash"] is not None
        and not db.execute(
            "SELECT 1 FROM generations WHERE session_id=? AND turn_no=?",
            (turn["session_id"], turn["turn_no"]),
        ).fetchone()
    ):
        return False
    row = db.execute(
        """SELECT s.*,m.consent,m.opt_out,c.allowed,c.enabled,
           COALESCE(e.epoch,0) AS epoch FROM sessions s
           JOIN chats c ON c.chat_id=s.chat_id
           JOIN members m ON m.chat_id=s.chat_id AND m.user_id=s.target_id
           LEFT JOIN data_epochs e ON e.chat_id=s.chat_id AND e.target_id=s.target_id
           WHERE s.id=?""",
        (turn["session_id"],),
    ).fetchone()
    return bool(
        row
        and row["state"] == "active"
        and row["allowed"]
        and row["enabled"]
        and row["consent"]
        and not row["opt_out"]
        and row["revision"] == turn["revision"]
        and (turn["evidence_epoch"] is None or row["epoch"] == turn["evidence_epoch"])
        and turn["turn_no"] == row["turn_count"] + 1
        and row["turn_count"] < 4
        and now < row["created_at"] + 300
        and (row["last_sent_at"] is None or now < row["last_sent_at"] + 90)
        and (turn["deadline"] is None or now < turn["deadline"])
        and sources_current(
            db,
            row["chat_id"],
            row["target_id"],
            json.loads(turn["evidence_ids"]) if turn["evidence_ids"] else [],
            now,
        )
    )


def cancel_turn(db: sqlite3.Connection, session_id: str, turn_no: int) -> None:
    db.execute(
        "UPDATE turns SET status='cancelled' WHERE session_id=? AND turn_no=? "
        "AND status IN ('pending','queued','generating','ready')",
        (session_id, turn_no),
    )
    db.execute(
        "UPDATE outbox SET status='cancelled' WHERE session_id=? AND turn_no=? AND status='ready'",
        (session_id, turn_no),
    )
