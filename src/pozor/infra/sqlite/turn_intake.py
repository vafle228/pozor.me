"""Receipt deduplication, admission and periodic invalidation of durable legacy turns."""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.delivery_state import cancel_turn, guard


@dataclass(frozen=True)
class TurnAdmission:
    accepted: bool
    rebuilds: int
    last_sent_at: float | None


class TurnIntake:
    def __init__(self, database: Path, clock: Callable[[], float]):
        self.database, self.clock = database, clock

    def chat_enabled(self, chat_id: int) -> bool:
        with connect(self.database) as db:
            return (
                db.execute(
                    "SELECT 1 FROM chats WHERE chat_id=? AND allowed=1 AND enabled=1", (chat_id,)
                ).fetchone()
                is not None
            )

    def accept(
        self, session_id: str, message_id: int, text: str, author_id: int | None, rebuilds: int
    ) -> TurnAdmission | None:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            s = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if s is None or s["state"] != "active" or s["turn_count"] >= 4:
                return None
            receipt = db.execute(
                "INSERT OR IGNORE INTO interaction_receipts VALUES (?,?)",
                (s["chat_id"], message_id),
            )
            if not receipt.rowcount:
                return None
            number = s["turn_count"] + 1
            old = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_no=?", (session_id, number)
            ).fetchone()
            if old and old["status"] in {"sending", "sent", "unknown"}:
                return None
            if old and old["status"] == "generating":
                rebuilds += 1
                if rebuilds > 2:
                    cancel_turn(db, session_id, number)
                    return TurnAdmission(False, rebuilds, s["last_sent_at"])
            revision = s["revision"] + 1
            db.execute("UPDATE sessions SET revision=? WHERE id=?", (revision, session_id))
            if old:
                if old["status"] in {"cancelled", "failed"}:
                    db.execute(
                        "DELETE FROM turn_inputs WHERE session_id=? AND turn_no=?",
                        (session_id, number),
                    )
                cancel_turn(db, session_id, number)
                db.execute(
                    "UPDATE turns SET revision=?,evidence_epoch=NULL,deadline=NULL,"
                    "status='pending',text=NULL,evidence_ids=NULL,context_hash=NULL "
                    "WHERE session_id=? AND turn_no=?",
                    (revision, session_id, number),
                )
            else:
                db.execute(
                    "INSERT INTO turns(session_id,turn_no,revision,status) VALUES (?,?,?,'pending')",
                    (session_id, number, revision),
                )
            db.execute(
                "INSERT INTO turn_inputs(session_id,turn_no,source_message_id,text,author_id) "
                "VALUES (?,?,?,?,?)",
                (session_id, number, message_id, text, author_id),
            )
        return TurnAdmission(True, rebuilds, s["last_sent_at"])

    def invalidate(self, keys: Collection[str], sessions: Collection[str]) -> list[str]:
        cancelled: list[str] = []
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            for key in keys:
                if key.startswith(("profile:", "hooks:")):
                    _, chat, target, *_ = key.split(":")
                    member = db.execute(
                        "SELECT 1 FROM members m WHERE chat_id=? AND user_id=? "
                        "AND consent=1 AND opt_out=0 AND NOT EXISTS "
                        "(SELECT 1 FROM forgotten_members f WHERE f.chat_id=m.chat_id "
                        "AND f.user_id=m.user_id)",
                        (int(chat), int(target)),
                    ).fetchone()
                    if not member:
                        cancelled.append(key)
            for sid in sessions:
                active = db.execute(
                    "SELECT 1 FROM sessions WHERE id=? AND state='active'", (sid,)
                ).fetchone()
                if not active:
                    cancelled.append(f"session:{sid}")
            for turn in db.execute(
                "SELECT * FROM turns WHERE status IN ('pending','queued','generating','ready')"
            ).fetchall():
                if not guard(db, turn, self.clock()):
                    cancel_turn(db, turn["session_id"], turn["turn_no"])
                    cancelled.append(f"session:{turn['session_id']}")
        return cancelled
