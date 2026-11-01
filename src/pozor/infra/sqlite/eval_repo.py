import hashlib
import time
from pathlib import Path

from pozor.core.domain.context import ContextPack
from pozor.core.domain.evaluation import EvalCase
from pozor.infra.sqlite.connection import connect, migrate
from pozor.infra.sqlite.retrieval import retrieve


def prepare(database: Path, case: EvalCase) -> ContextPack:
    migrate(database)
    with connect(database) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,1,1)")
        for number, quote in enumerate(case.quotes, 1):
            attr = (
                "observed_self"
                if quote.author == quote.target and quote.relation == "self"
                else "third_party"
            )
            db.execute(
                "INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,"
                "author_id,text,text_hash,occurred_at,origin) VALUES (?,?,?,?,?,?,?,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'live')",
                (
                    number,
                    quote.chat,
                    f"live:{quote.chat}",
                    number,
                    quote.author,
                    quote.text,
                    hashlib.sha256(quote.text.encode()).hexdigest(),
                ),
            )
            db.execute(
                "INSERT INTO message_subjects VALUES (?,?,?,?)",
                (number, quote.target, quote.relation, attr),
            )
    return retrieve(
        database,
        chat_id=-1,
        thread_id=0,
        target_id=42,
        trigger_id=999,
        hint=case.hint,
        target_label="Synthetic 42",
    )


def control_check(database: Path, case: EvalCase) -> bool | None:
    if case.action == "none":
        return None
    from pozor.infra.sqlite.commands import close_session, set_consent, start_session
    from pozor.infra.sqlite.delivery import guard
    from pozor.infra.sqlite.privacy_repo import bump, forget

    now = int(time.time())
    with connect(database) as db:
        db.execute("INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1)")
    sid = start_session(
        database,
        chat_id=-1,
        thread_id=0,
        target_id=42,
        initiator_id=7,
        trigger_message_id=999,
        now=now,
    )
    with connect(database) as db:
        db.execute(
            "INSERT INTO turns(session_id,turn_no,revision,evidence_epoch,deadline,status) "
            "SELECT id,1,revision,0,?,'ready' FROM sessions WHERE id=?",
            (now + 8, sid),
        )
        turn = db.execute("SELECT * FROM turns WHERE session_id=?", (sid,)).fetchone()
        if not guard(db, turn, now):
            return False
    if case.action == "stop":
        close_session(database, session_id=sid, chat_id=-1, thread_id=0, actor_id=42)
    elif case.action == "edit":
        with connect(database) as db:
            bump(db, -1, 42)
    elif case.action == "forget":
        forget(database, -1, 42)
    elif case.action == "optout":
        set_consent(database, -1, 42, False)
    with connect(database) as db:
        return not guard(db, turn, now + 9 if case.action == "deadline" else now)


