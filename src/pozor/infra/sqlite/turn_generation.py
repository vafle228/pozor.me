"""SQLite snapshots and revision-checked transitions for legacy turn generation."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pozor.core.domain.context import ContextPack, ConversationTurn
from pozor.core.domain.generation import GenerationResult
from pozor.core.usecases.generate_turn import TurnRevision
from pozor.infra.config.registry import Settings
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.delivery_state import guard
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.sqlite.hook_repo import select_hooks
from pozor.infra.sqlite.retrieval import retrieve, sources_current


@dataclass(frozen=True)
class PendingTurn:
    turn: TurnRevision
    target_id: int
    target_label: str
    initiator_id: int
    speaker_id: int | None
    trigger_id: int
    hint: str
    mode: str
    level: int
    sent_texts: list[str]
    conversation: list[ConversationTurn]


class TurnGenerationRepository:
    def __init__(
        self,
        database: Path,
        journal: GenerationRepository,
        settings: Callable[[], Settings],
        clock: Callable[[], float],
    ):
        self.database, self.journal, self.settings, self.clock = database, journal, settings, clock

    def scope(self, session_id: str) -> tuple[int, int] | None:
        with connect(self.database) as db:
            row = db.execute(
                "SELECT chat_id,thread_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return (row["chat_id"], row["thread_id"]) if row else None

    def pending(self, session_id: str) -> PendingTurn | None:
        with connect(self.database) as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            turn = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND status='pending'", (session_id,)
            ).fetchone()
            if session is None or turn is None or not guard(db, turn, self.clock()):
                return None
            inputs = db.execute(
                "SELECT * FROM turn_inputs WHERE session_id=? AND turn_no=? ORDER BY source_message_id",
                (session_id, turn["turn_no"]),
            ).fetchall()
            member = db.execute(
                "SELECT display_name FROM members WHERE chat_id=? AND user_id=?",
                (session["chat_id"], session["target_id"]),
            ).fetchone()
            conversation = []
            sent_texts = []
            for previous in db.execute(
                "SELECT turn_no,text,fallback FROM turns WHERE session_id=? AND status='sent' "
                "ORDER BY turn_no",
                (session_id,),
            ):
                sent_texts.append(previous["text"])
                for entry in db.execute(
                    "SELECT text,author_id FROM turn_inputs WHERE session_id=? AND turn_no=? "
                    "ORDER BY source_message_id",
                    (session_id, previous["turn_no"]),
                ):
                    conversation.append(
                        ConversationTurn(
                            role="user",
                            author_id=entry["author_id"],
                            text=entry["text"],
                        )
                    )
                if not previous["fallback"]:
                    conversation.append(ConversationTurn(role="assistant", text=previous["text"]))
        return PendingTurn(
            turn=TurnRevision(
                session_id,
                turn["turn_no"],
                turn["revision"],
                session["chat_id"],
                session["thread_id"],
            ),
            target_id=session["target_id"],
            target_label=(member["display_name"] if member else None) or "Участник",
            initiator_id=session["initiator_id"],
            speaker_id=inputs[-1]["author_id"],
            trigger_id=inputs[-1]["source_message_id"],
            hint="\n".join(r["text"] for r in inputs),
            mode=session["mode"],
            level=session["roast_level"],
            sent_texts=sent_texts,
            conversation=conversation,
        )

    async def context(self, pending: PendingTurn, settings: Settings) -> ContextPack:
        chat = pending.turn.chat_id
        target = pending.target_id
        level = min(settings.roast_level, pending.level)
        pack = await asyncio.to_thread(
            retrieve,
            self.database,
            chat_id=chat,
            thread_id=pending.turn.thread_id,
            target_id=target,
            trigger_id=pending.trigger_id,
            hint=pending.hint,
            target_label=pending.target_label,
            weights=settings.retrieval_weights,
            cards_enabled=settings.profile_cards_enabled,
            min_terms=settings.retrieval_min_terms,
        )
        angles, answers, sources, examples = await asyncio.to_thread(
            self.journal.memory, chat, target
        )
        mechanisms = await asyncio.to_thread(self.journal.mechanisms, chat)
        hooks = await asyncio.to_thread(
            select_hooks, self.database, chat, target, pack.trigger.text, level, sources
        )
        return pack.model_copy(
            update={
                "session_turns": list(dict.fromkeys([*answers, *pending.sent_texts])),
                "mode": pending.mode,
                "forbidden_angles": angles,
                "few_shot": examples,
                "hooks": hooks,
                "repetition_texts": answers,
                "recent_mechanisms": mechanisms,
                "level": level,
                "conversation": pending.conversation,
                "initiator_id": pending.initiator_id,
                "speaker_id": pending.speaker_id,
                "trigger": pack.trigger.model_copy(update={"author_id": pending.speaker_id}),
            }
        )

    def set_deadline(self, turn: TurnRevision, deadline: float) -> None:
        with connect(self.database) as db:
            db.execute(
                "UPDATE turns SET deadline=? WHERE session_id=? AND turn_no=? "
                "AND revision=? AND status='pending'",
                (deadline, turn.session_id, turn.number, turn.revision),
            )

    def mark_queued(self, turn: TurnRevision, epoch: int, deadline: float) -> bool:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE turns SET evidence_epoch=?,deadline=?,status='queued' WHERE session_id=? "
                "AND turn_no=? AND revision=? AND status='pending'",
                (epoch, deadline, turn.session_id, turn.number, turn.revision),
            )
            current = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_no=?",
                (turn.session_id, turn.number),
            ).fetchone()
            return bool(
                current
                and current["revision"] == turn.revision
                and current["status"] == "queued"
                and guard(db, current, self.clock())
            )

    def begin_inference(self, turn: TurnRevision, pack: ContextPack) -> bool:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_no=?",
                (turn.session_id, turn.number),
            ).fetchone()
            ids = [e.evidence_id for e in [*pack.recent, *pack.evidence]]
            ids.extend(eid for memory in pack.dossier for eid in memory.evidence_ids)
            if (
                row is None
                or row["revision"] != turn.revision
                or row["status"] != "queued"
                or not guard(db, row, self.clock())
                or not sources_current(db, pack.chat_id, pack.target_id, ids, self.clock())
            ):
                return False
            db.execute(
                "UPDATE turns SET status='generating' WHERE session_id=? AND turn_no=?",
                (turn.session_id, turn.number),
            )
            return True

    def roast_limit_reached(self, pack: ContextPack) -> bool:
        limit = self.settings().target_daily_round_limit
        with connect(self.database) as db:
            count = db.execute(
                "SELECT coalesce(sum(turn_count),0) FROM sessions WHERE chat_id=? AND target_id=? "
                "AND mode='roast' AND created_at>?",
                (pack.chat_id, pack.target_id, self.clock() - 86400),
            ).fetchone()[0]
        return bool(count >= limit)

    async def record(
        self,
        turn: TurnRevision,
        pack: ContextPack,
        result: GenerationResult,
    ) -> int | None:
        return await asyncio.to_thread(
            self.journal.record,
            pack,
            result,
            self.settings(),
            turn.session_id,
            turn.number,
            turn.revision,
        )

    def store_result(self, turn: TurnRevision, result: GenerationResult) -> None:
        sid, number = turn.session_id, turn.number
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND turn_no=?", (sid, number)
            ).fetchone()
            if (
                row is None
                or row["revision"] != turn.revision
                or row["status"] != "generating"
                or not guard(db, row, self.clock())
            ):
                return
            if not db.execute(
                "SELECT 1 FROM generations WHERE id=? AND session_id=? AND turn_no=?",
                (result.generation_id, sid, number),
            ).fetchone():
                return
            session = db.execute(
                "SELECT chat_id,target_id FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if not sources_current(
                db,
                session["chat_id"],
                session["target_id"],
                result.roast.evidence_ids,
                self.clock(),
            ):
                return
            db.execute(
                "UPDATE turns SET status='ready',text=?,evidence_ids=?,context_hash=?,token_count=?,"
                "generation_reason=?,fallback=?,deadline=? WHERE session_id=? AND turn_no=?",
                (
                    result.roast.text,
                    json.dumps(result.roast.evidence_ids),
                    result.context_hash,
                    result.token_count,
                    result.reason,
                    int(result.fallback),
                    self.clock() + 5,
                    sid,
                    number,
                ),
            )
            db.execute(
                "INSERT INTO outbox(session_id,turn_no,status,next_attempt_at) VALUES (?,?,'ready',?) "
                "ON CONFLICT(session_id,turn_no) DO UPDATE SET status='ready',"
                "next_attempt_at=excluded.next_attempt_at,error_code=NULL",
                (sid, number, self.clock()),
            )

    def fail(self, turn: TurnRevision, reason: str) -> None:
        with connect(self.database) as db:
            db.execute(
                "UPDATE turns SET status='failed',generation_reason=? WHERE session_id=? AND turn_no=? "
                "AND revision=? AND status IN ('pending','queued','generating')",
                (reason, turn.session_id, turn.number, turn.revision),
            )

    def fail_job(self, session_id: str, reason: str) -> None:
        with connect(self.database) as db:
            db.execute(
                "UPDATE turns SET status='failed',generation_reason=? WHERE session_id=? "
                "AND status IN ('pending','queued','generating')",
                (reason, session_id),
            )

    def has_pending(self, session_id: str) -> bool:
        with connect(self.database) as db:
            return (
                db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND status='pending'", (session_id,)
                ).fetchone()
                is not None
            )
