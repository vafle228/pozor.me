"""Read the round and sent-reply identity together before acting on an event."""

from pathlib import Path
from typing import Literal

from pozor.core.usecases.legacy_round import ActiveRound
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.privacy_repo import blocked


def participant_target(database: Path, chat: int, thread: int, actor: int) -> int | None:
    with connect(database) as db:
        row = db.execute(
            """SELECT target_id FROM sessions WHERE chat_id=? AND thread_id=?
               AND (target_id=? OR initiator_id=?) AND state='active'""",
            (chat, thread, actor, actor),
        ).fetchone()
        return int(row[0]) if row else None


def edited_input(database: Path, chat: int, message: int) -> None:
    with connect(database) as db:
        db.execute(
            """UPDATE sessions SET revision=revision+1 WHERE chat_id=? AND id IN
               (SELECT session_id FROM turn_inputs WHERE source_message_id=?)""",
            (chat, message),
        )


def round_for_message(
    database: Path, chat: int, thread: int, actor: int, reply: int | None
) -> ActiveRound | Literal["closed"] | None:
    with connect(database) as db:
        if blocked(db, chat, actor):
            return None
        row = db.execute(
            """SELECT s.id,s.target_id,s.initiator_id,s.trigger_message_id,s.mode,m.display_name,
                      EXISTS(SELECT 1 FROM outbox o WHERE o.session_id=s.id AND o.status='sent'
                             AND o.telegram_message_id=?) AS direct_reply
               FROM sessions s LEFT JOIN members m ON m.chat_id=s.chat_id AND m.user_id=s.target_id
               WHERE s.chat_id=? AND s.thread_id=? AND s.state='active'""",
            (reply, chat, thread),
        ).fetchone()
        if row:
            return ActiveRound(
                row["id"],
                row["target_id"],
                row["initiator_id"],
                row["trigger_message_id"],
                row["display_name"] or "Участник",
                row["mode"],
                bool(row["direct_reply"]),
            )
        if (
            reply is not None
            and db.execute(
                """SELECT 1 FROM outbox o JOIN sessions s ON s.id=o.session_id
               WHERE s.chat_id=? AND s.thread_id=? AND o.telegram_message_id=? AND o.status='sent'
               AND s.state='closed' AND (s.target_id=? OR s.initiator_id=?)""",
                (chat, thread, reply, actor, actor),
            ).fetchone()
        ):
            return "closed"
        return None
