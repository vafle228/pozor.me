"""Atomic claim and completion of a legacy outbox row."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pozor.core.usecases.deliver_turn import OutgoingTurn, SendResult
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.delivery_state import cancel_turn, guard


class TurnOutbox:
    def __init__(self, database: Path, clock: Callable[[], float]):
        self.database = database
        self.clock = clock

    def claim(self, now: float) -> OutgoingTurn | Literal["cancelled"] | None:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT t.*,s.chat_id,s.thread_id FROM outbox o "
                "JOIN turns t USING(session_id,turn_no) JOIN sessions s ON s.id=o.session_id "
                "WHERE o.status='ready' AND o.next_attempt_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM delivery_limits l "
                "WHERE l.scope='chat:' || s.chat_id AND l.next_at>?) "
                "ORDER BY o.next_attempt_at LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            sid, number, chat_id = row["session_id"], row["turn_no"], row["chat_id"]
            if not guard(db, row, now):
                cancel_turn(db, sid, number)
                return "cancelled"
            scopes = (("global", 1.0), (f"chat:{chat_id}", 3.0))
            for scope, _ in scopes:
                limit = db.execute(
                    "SELECT next_at FROM delivery_limits WHERE scope=?", (scope,)
                ).fetchone()
                if limit and limit[0] > now:
                    return None
            for scope, gap in scopes:
                db.execute(
                    "INSERT INTO delivery_limits VALUES (?,?) ON CONFLICT(scope) "
                    "DO UPDATE SET next_at=excluded.next_at",
                    (scope, now + gap),
                )
            db.execute(
                "UPDATE outbox SET status='sending' WHERE session_id=? AND turn_no=?", (sid, number)
            )
            db.execute(
                "UPDATE turns SET status='sending' WHERE session_id=? AND turn_no=?", (sid, number)
            )
            trigger = db.execute(
                "SELECT source_message_id FROM turn_inputs WHERE session_id=? AND turn_no=? "
                "ORDER BY source_message_id DESC LIMIT 1",
                (sid, number),
            ).fetchone()[0]
            rating = db.execute(
                "SELECT id FROM generations WHERE session_id=? AND turn_no=? AND mode='roast'",
                (sid, number),
            ).fetchone()
            return OutgoingTurn(
                sid,
                number,
                chat_id,
                row["thread_id"],
                trigger,
                row["text"],
                row["deadline"],
                rating[0] if rating else None,
            )

    def finish(self, turn: OutgoingTurn, result: SendResult, retry_at: float) -> None:
        sid, number = turn.session_id, turn.turn_no
        status, error, message_id = result.status, result.error, result.message_id
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_no=?", (sid, number)
            ).fetchone()
            if status == "ready" and (current is None or not guard(db, current, self.clock())):
                status = "cancelled"
            db.execute(
                "UPDATE outbox SET status=?,error_code=?,telegram_message_id=?,next_attempt_at=? "
                "WHERE session_id=? AND turn_no=?",
                (status, error, message_id, retry_at, sid, number),
            )
            db.execute(
                "UPDATE turns SET status=? WHERE session_id=? AND turn_no=?", (status, sid, number)
            )
            if status == "sent":
                db.execute(
                    "UPDATE sessions SET turn_count=turn_count+1,last_sent_at=? WHERE id=? "
                    "AND turn_count=?",
                    (int(self.clock()), sid, number - 1),
                )
                db.execute(
                    "UPDATE sessions SET state='closed',closed_reason='turn_limit',revision=revision+1 "
                    "WHERE id=? AND state='active' AND turn_count>=4",
                    (sid,),
                )
            if status == "unknown":
                db.execute(
                    "UPDATE sessions SET state='closed',closed_reason='delivery_unknown',revision=revision+1 "
                    "WHERE id=? AND state='active'",
                    (sid,),
                )
            if error == "403":
                db.execute(
                    "UPDATE chats SET enabled=0 WHERE chat_id=(SELECT chat_id FROM sessions WHERE id=?)",
                    (sid,),
                )
                db.execute(
                    "UPDATE sessions SET state='closed',closed_reason='forbidden',revision=revision+1 "
                    "WHERE chat_id=(SELECT chat_id FROM sessions WHERE id=?) AND state='active'",
                    (sid,),
                )
