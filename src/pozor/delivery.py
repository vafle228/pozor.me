"""T5 durable turns and conservative Telegram delivery, owned by one process.

SQLite guards authorize a send, never the LLM. An ambiguous network result is
terminal: Bot API has no sendMessage idempotency key. No network inside a DB lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from pozor.commands import (
    CommandContext,
    callback_data,
    close_session,
    close_sessions_on_startup,
    expire_sessions,
    handle_telegram_update,
    parse_command,
    resolve_target,
    roast_hint,
)
from pozor.config import Settings
from pozor.context import ContextPack, ConversationTurn, retrieve, sources_current
from pozor.inference_queue import InferenceQueue, QueueBusy
from pozor.infra.config.service import SettingsService
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.sqlite.hook_repo import select_hooks
from pozor.llm import GenerationResult, InferenceError
from pozor.storage import connect
from pozor.telegram_scope import topic_id

logger = logging.getLogger(__name__)


class RoastGenerator(Protocol):
    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult: ...


def recover_delivery(database: Path) -> None:
    """Crash after possible delivery must not cause a blind resend."""
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE outbox SET status='unknown',error_code='restart' WHERE status='sending'")
        db.execute("UPDATE turns SET status='unknown' WHERE status='sending'")
        db.execute("UPDATE outbox SET status='cancelled' WHERE status='ready'")
        db.execute("UPDATE turns SET status='cancelled' WHERE status IN ('pending','queued','generating','ready')")
    close_sessions_on_startup(database)


def guard(db: sqlite3.Connection, turn: sqlite3.Row, now: float) -> bool:
    """Production equivalent of may_publish, plus chat and durable consent guards."""
    row = db.execute(
        """SELECT s.*,m.consent,m.opt_out,c.allowed,c.enabled,
           COALESCE(e.epoch,0) AS epoch FROM sessions s
           JOIN chats c ON c.chat_id=s.chat_id
           JOIN members m ON m.chat_id=s.chat_id AND m.user_id=s.target_id
           LEFT JOIN data_epochs e ON e.chat_id=s.chat_id AND e.target_id=s.target_id
           WHERE s.id=?""", (turn["session_id"],),
    ).fetchone()
    return bool(row and row["state"] == "active" and row["allowed"] and row["enabled"]
                and row["consent"] and not row["opt_out"]
                and row["revision"] == turn["revision"]
                and (turn["evidence_epoch"] is None or row["epoch"] == turn["evidence_epoch"])
                and turn["turn_no"] == row["turn_count"] + 1 and row["turn_count"] < 4
                and now < row["created_at"] + 300
                and (row["last_sent_at"] is None or now < row["last_sent_at"] + 90)
                and (turn["deadline"] is None or now < turn["deadline"])
                and sources_current(db, row['chat_id'], row['target_id'],
                                    json.loads(turn['evidence_ids']) if turn['evidence_ids'] else [], now))


def cancel_turn(db: sqlite3.Connection, session_id: str, turn_no: int) -> None:
    db.execute("UPDATE turns SET status='cancelled' WHERE session_id=? AND turn_no=? "
               "AND status IN ('pending','queued','generating','ready')", (session_id, turn_no))
    db.execute("UPDATE outbox SET status='cancelled' WHERE session_id=? AND turn_no=? "
               "AND status='ready'", (session_id, turn_no))


class DeliveryPipeline:
    def __init__(
        self, database: Path, settings: Settings, bot: Any, generator: RoastGenerator,
        queue: InferenceQueue, *, clock: Callable[[], float] = time.time,
    ) -> None:
        self.database, self.settings, self.bot = database, settings, bot
        self.generator, self.queue, self.clock = generator, queue, clock
        self.generation_log = GenerationRepository(database)
        self.settings_service: SettingsService | None = None
        self.ready = False
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.due: dict[str, tuple[float, float]] = {}
        self.rebuilds: dict[str, int] = {}
        self.notice_at: dict[int, float] = {}
        self.stopping = False

    async def notify(self, chat_id: int, thread_id: int, text: str) -> None:
        # Shared cooldown for queue-full and health notices. No archive/prompt in logs.
        now = self.clock()
        with connect(self.database) as db:
            allowed = db.execute("SELECT 1 FROM chats WHERE chat_id=? AND allowed=1 AND enabled=1",
                                 (chat_id,)).fetchone()
        if not allowed:
            return
        if now < self.notice_at.get(chat_id, 0):
            return
        self.notice_at[chat_id] = now + 30
        try:
            async with asyncio.timeout(2):
                await self.bot.send_message(chat_id=chat_id, message_thread_id=thread_id or None,
                                            text=text, parse_mode=None)
        except (TelegramAPIError, OSError, TimeoutError):
            logger.warning("delivery_notice_failed chat_id=%s", chat_id)

    def accept(self, session_id: str, message_id: int, text: str, *, first: bool = False,
               author_id: int | None = None) -> bool:
        """Persist/coalesce one turn; never wait for the GPU in the update handler."""
        if self.stopping or not self.ready:
            return False
        now = self.clock()
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            s = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if s is None or s["state"] != "active" or s["turn_count"] >= 4:
                return False
            receipt = db.execute("INSERT OR IGNORE INTO interaction_receipts VALUES (?,?)",
                                 (s["chat_id"], message_id))
            if not receipt.rowcount:
                return False
            number = s["turn_count"] + 1
            old = db.execute("SELECT * FROM turns WHERE session_id=? AND turn_no=?",
                             (session_id, number)).fetchone()
            if old and old["status"] in {"sending", "sent", "unknown"}:
                return False
            if old and old["status"] == "generating":
                self.rebuilds[session_id] = self.rebuilds.get(session_id, 0) + 1
                if self.rebuilds[session_id] > 2:
                    cancel_turn(db, session_id, number)
                    self.queue.cancel(f"session:{session_id}")
                    return False
            revision = s["revision"] + 1
            db.execute("UPDATE sessions SET revision=? WHERE id=?", (revision, session_id))
            if old:
                if old["status"] in {"cancelled", "failed"}:
                    db.execute("DELETE FROM turn_inputs WHERE session_id=? AND turn_no=?",
                               (session_id, number))
                cancel_turn(db, session_id, number)
                db.execute("UPDATE turns SET revision=?,evidence_epoch=NULL,deadline=NULL,"
                           "status='pending',text=NULL,evidence_ids=NULL,context_hash=NULL "
                           "WHERE session_id=? AND turn_no=?", (revision, session_id, number))
            else:
                db.execute("INSERT INTO turns(session_id,turn_no,revision,status) VALUES (?,?,?,'pending')",
                           (session_id, number, revision))
            db.execute("INSERT INTO turn_inputs(session_id,turn_no,source_message_id,text,author_id) "
                       "VALUES (?,?,?,?,?)", (session_id, number, message_id, text, author_id))
        loop = asyncio.get_running_loop()
        began = self.due.get(session_id, (loop.time(), 0))[0]
        due = loop.time() if first else min(loop.time() + 1.2, began + 3)
        # Fast replies wait for the ten-second product cooldown, not a new round.
        if s["last_sent_at"] is not None:
            due = max(due, loop.time() + max(0, s["last_sent_at"] + 10 - now))
        self.due[session_id] = (began, due)
        self.queue.cancel(f"session:{session_id}")
        if session_id not in self.jobs or self.jobs[session_id].done():
            self.rebuilds[session_id] = 0
            self.jobs[session_id] = asyncio.create_task(self._drive(session_id))
            self.jobs[session_id].add_done_callback(lambda _: self.jobs.pop(session_id, None))
        return True

    async def _drive(self, session_id: str) -> None:
        typing: asyncio.Task[None] | None = None
        try:
            if hasattr(self.bot, 'send_chat_action'):
                with connect(self.database) as db:
                    scope = db.execute('SELECT chat_id,thread_id FROM sessions WHERE id=?',
                                       (session_id,)).fetchone()
                if scope:
                    typing = asyncio.create_task(self._typing(scope['chat_id'], scope['thread_id']))
            while not self.stopping:
                settings = await asyncio.to_thread(self.settings_service.snapshot) if self.settings_service else self.settings
                delay = self.due[session_id][1] - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(min(delay, .1))
                    continue
                with connect(self.database) as db:
                    s = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                    turn = db.execute("SELECT * FROM turns WHERE session_id=? AND status='pending'",
                                      (session_id,)).fetchone()
                    if s is None or turn is None or not guard(db, turn, self.clock()):
                        return
                    inputs = db.execute("SELECT * FROM turn_inputs WHERE session_id=? AND turn_no=? "
                                        "ORDER BY source_message_id", (session_id, turn["turn_no"])).fetchall()
                    memory = [r[0] for r in db.execute("SELECT text FROM turns WHERE session_id=? "
                                                     "AND status='sent' ORDER BY turn_no", (session_id,))]
                    member = db.execute("SELECT display_name FROM members WHERE chat_id=? AND user_id=?",
                                        (s['chat_id'], s['target_id'])).fetchone()
                    conversation = []
                    for previous in db.execute("SELECT turn_no,text,fallback FROM turns WHERE session_id=? "
                                               "AND status='sent' ORDER BY turn_no", (session_id,)):
                        for entry in db.execute("SELECT text,author_id FROM turn_inputs WHERE session_id=? "
                                                "AND turn_no=? ORDER BY source_message_id",
                                                (session_id, previous['turn_no'])):
                            conversation.append(ConversationTurn(role='user', author_id=entry['author_id'],
                                                                 text=entry['text']))
                        if not previous['fallback']:
                            conversation.append(ConversationTurn(role='assistant', text=previous['text']))
                # Absolute deadline includes retrieval, queue, tokenization and validation.
                deadline = asyncio.get_running_loop().time() + settings.generation_deadline_seconds
                wall_deadline = self.clock() + settings.generation_deadline_seconds
                revision, number = turn["revision"], turn["turn_no"]
                with connect(self.database) as db:
                    db.execute("UPDATE turns SET deadline=? WHERE session_id=? AND turn_no=? "
                               "AND revision=? AND status='pending'",
                               (wall_deadline, session_id, number, revision))
                try:
                    async with asyncio.timeout_at(deadline):
                        pack = await asyncio.to_thread(
                            retrieve, self.database, chat_id=s["chat_id"], thread_id=s["thread_id"],
                            target_id=s["target_id"], trigger_id=inputs[-1]["source_message_id"],
                            hint="\n".join(r["text"] for r in inputs),
                            target_label=member['display_name'] if member and member['display_name'] else 'Участник',
                            weights=settings.retrieval_weights,
                            cards_enabled=settings.profile_cards_enabled, min_terms=settings.retrieval_min_terms,
                        )
                        angles, previous_answers, used_sources, examples = await asyncio.to_thread(
                            self.generation_log.memory, s['chat_id'], s['target_id'])
                        hooks = await asyncio.to_thread(select_hooks,self.database,s['chat_id'],s['target_id'],
                            pack.trigger.text,min(settings.roast_level,s['roast_level']),used_sources)
                        pack = pack.model_copy(update={"session_turns": list(dict.fromkeys([*previous_answers,*memory])), "mode": s["mode"],
                            "forbidden_angles": angles, "few_shot": examples, "hooks": hooks,
                            "level": min(settings.roast_level,s["roast_level"]),
                            "conversation": conversation, "initiator_id": s['initiator_id'],
                            "speaker_id": inputs[-1]['author_id'],
                            "trigger": pack.trigger.model_copy(update={'author_id': inputs[-1]['author_id']})})
                        with connect(self.database) as db:
                            db.execute("BEGIN IMMEDIATE")
                            db.execute("UPDATE turns SET evidence_epoch=?,deadline=?,status='queued' "
                                       "WHERE session_id=? AND turn_no=? AND revision=? AND status='pending'",
                                       (pack.evidence_epoch, wall_deadline, session_id, number, revision))
                            current = db.execute("SELECT * FROM turns WHERE session_id=? AND turn_no=?",
                                                 (session_id, number)).fetchone()
                            if (current is None or current["revision"] != revision or current["status"] != "queued"
                                    or not guard(db, current, self.clock())):
                                continue
                        result: GenerationResult = await self.queue.submit(
                            key=f"session:{session_id}", chat_id=s["chat_id"], deadline=deadline,
                            factory=partial(self._generate, pack, session_id, number, revision),
                        )
                    self._store_result(session_id, number, revision, result)
                except asyncio.CancelledError:
                    if self.stopping or asyncio.current_task().cancelling():  # type: ignore[union-attr]
                        raise
                except (TimeoutError, InferenceError, QueueBusy) as exc:
                    reason = 'deadline' if isinstance(exc, TimeoutError) else str(exc)
                    if reason.startswith('stale_'):
                        return
                    logger.warning('generation_failed reason=%s', reason)
                    if isinstance(exc, QueueBusy):
                        await self.notify(s["chat_id"], s["thread_id"], "Сейчас занят. Попробуйте позже.")
                    with connect(self.database) as db:
                        db.execute("UPDATE turns SET status='failed',generation_reason=? WHERE session_id=? AND turn_no=? "
                                   "AND revision=? AND status IN ('pending','queued','generating')", (reason, session_id, number, revision))
                    if not isinstance(exc, QueueBusy):
                        await self.notify(s['chat_id'], s['thread_id'],
                                          'Ответ не успел подготовиться. Раунд сохранён; можно продолжить тегом или ответом.')
                with connect(self.database) as db:
                    pending = db.execute("SELECT 1 FROM turns WHERE session_id=? AND status='pending'",
                                         (session_id,)).fetchone()
                if not pending:
                    return
        except Exception as exc:  # noqa: BLE001 -- persist safe diagnostics, never exception bodies
            logger.error("generation_job_failed session_id=%s type=%s", session_id, type(exc).__name__)
            with connect(self.database) as db:
                db.execute("UPDATE turns SET status='failed',generation_reason=? WHERE session_id=? "
                           "AND status IN ('pending','queued','generating')",
                           ('job_error:' + type(exc).__name__, session_id))
        finally:
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
            self.due.pop(session_id, None)
            self.rebuilds.pop(session_id, None)

    async def _typing(self, chat_id: int, thread_id: int) -> None:
        while True:
            try:
                async with asyncio.timeout(2):
                    await self.bot.send_chat_action(chat_id=chat_id, message_thread_id=thread_id or None,
                                                    action='typing')
            except (TelegramAPIError, OSError, TimeoutError):
                pass
            await asyncio.sleep(4)

    async def _generate(self, pack: ContextPack, sid: str, number: int,
                        revision: int, deadline: float) -> GenerationResult:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE session_id=? AND turn_no=?", (sid, number)).fetchone()
            if (turn is None or turn["revision"] != revision or turn["status"] != "queued"
                    or not guard(db, turn, self.clock())
                    or not sources_current(db, pack.chat_id, pack.target_id,
                                           [e.evidence_id for e in [*pack.recent, *pack.evidence]] +
                                           [eid for memory in pack.dossier for eid in memory.evidence_ids], self.clock())):
                raise InferenceError("stale_before_inference")
            db.execute("UPDATE turns SET status='generating' WHERE session_id=? AND turn_no=?", (sid, number))
        if pack.mode == 'roast':
            limit = self.settings_service.snapshot().target_daily_round_limit if self.settings_service else self.settings.target_daily_round_limit
            with connect(self.database) as db:
                sent = db.execute("SELECT coalesce(sum(turn_count),0) FROM sessions WHERE chat_id=? AND target_id=? "
                    "AND mode='roast' AND created_at>?",(pack.chat_id,pack.target_id,self.clock()-86400)).fetchone()[0]
            if sent >= limit:
                raise InferenceError('daily_round_limit')
        started = asyncio.get_running_loop().time()
        result = await self.generator.generate(pack, deadline)
        result = result.model_copy(update={'latency_ms': (asyncio.get_running_loop().time()-started)*1000})
        generation_id = await asyncio.to_thread(self.generation_log.record, pack, result,
            self.settings_service.snapshot() if self.settings_service else self.settings, sid, number)
        if generation_id is None:
            raise InferenceError('stale_after_inference')
        return result.model_copy(update={'generation_id': generation_id})

    def _store_result(self, sid: str, number: int, revision: int, result: GenerationResult) -> bool:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE session_id=? AND turn_no=?", (sid, number)).fetchone()
            if (turn is None or turn["revision"] != revision or turn["status"] != "generating"
                    or not guard(db, turn, self.clock())):
                return False
            session = db.execute('SELECT chat_id,target_id FROM sessions WHERE id=?', (sid,)).fetchone()
            if not sources_current(db, session['chat_id'], session['target_id'],
                                   result.roast.evidence_ids, self.clock()):
                return False
            db.execute("UPDATE turns SET status='ready',text=?,evidence_ids=?,context_hash=?,token_count=?,"
                       "generation_reason=?,fallback=?,deadline=? "
                       "WHERE session_id=? AND turn_no=?", (result.roast.text, json.dumps(result.roast.evidence_ids),
                       result.context_hash, result.token_count, result.reason, int(result.fallback),
                       self.clock() + 5, sid, number))
            db.execute("INSERT INTO outbox(session_id,turn_no,status,next_attempt_at) VALUES (?,?,'ready',?) "
                       "ON CONFLICT(session_id,turn_no) DO UPDATE SET status='ready',"
                       "next_attempt_at=excluded.next_attempt_at,error_code=NULL", (sid, number, self.clock()))
            return True

    def invalidate(self) -> None:
        """Cheap periodic guards also notice edits/rollback from the local import CLI."""
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            for key in list(self.queue.keys):
                if key.startswith(('profile:','hooks:')):
                    _, chat, target, *_ = key.split(':')
                    member = db.execute('SELECT 1 FROM members m WHERE chat_id=? AND user_id=? '
                                        'AND consent=1 AND opt_out=0 AND NOT EXISTS '
                                        '(SELECT 1 FROM forgotten_members f WHERE f.chat_id=m.chat_id '
                                        'AND f.user_id=m.user_id)', (int(chat), int(target))).fetchone()
                    if not member:
                        self.queue.cancel(key)
            for sid in list(self.jobs):
                active = db.execute("SELECT 1 FROM sessions WHERE id=? AND state='active'", (sid,)).fetchone()
                if not active:
                    self.queue.cancel(f"session:{sid}")
            for turn in db.execute("SELECT * FROM turns WHERE status IN ('pending','queued','generating','ready')").fetchall():
                if not guard(db, turn, self.clock()):
                    cancel_turn(db, turn["session_id"], turn["turn_no"])
                    self.queue.cancel(f"session:{turn['session_id']}")

    def rating_buttons(self, sid: str, number: int) -> list[list[InlineKeyboardButton]]:
        with connect(self.database) as db:
            generation = db.execute("SELECT id FROM generations WHERE session_id=? AND turn_no=? AND mode='roast'",
                                    (sid, number)).fetchone()
        if generation is None:
            return []
        return [[InlineKeyboardButton(text=label,callback_data=f'rate:{generation[0]}:{rating}')
                 for label,rating in [('🔥',1),('😐',0),('💩',-1)]]]

    async def send_once(self) -> bool:
        """Claim synchronously immediately before HTTP; no yield between guard and send."""
        now = self.clock()
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT t.*,s.chat_id,s.thread_id FROM outbox o "
                             "JOIN turns t USING(session_id,turn_no) JOIN sessions s ON s.id=o.session_id "
                             "WHERE o.status='ready' AND o.next_attempt_at<=? "
                             "AND NOT EXISTS (SELECT 1 FROM delivery_limits l "
                             "WHERE l.scope='chat:' || s.chat_id AND l.next_at>?) "
                             "ORDER BY o.next_attempt_at LIMIT 1",
                             (now, now)).fetchone()
            if row is None:
                return False
            sid, number, chat_id = row["session_id"], row["turn_no"], row["chat_id"]
            if not guard(db, row, now):
                cancel_turn(db, sid, number)
                return True
            scopes = (("global", 1.0), (f"chat:{chat_id}", 3.0))
            for scope, _ in scopes:
                limit = db.execute("SELECT next_at FROM delivery_limits WHERE scope=?", (scope,)).fetchone()
                if limit and limit[0] > now:
                    return False
            for scope, gap in scopes:
                db.execute("INSERT INTO delivery_limits VALUES (?,?) ON CONFLICT(scope) "
                           "DO UPDATE SET next_at=excluded.next_at", (scope, now + gap))
            db.execute("UPDATE outbox SET status='sending' WHERE session_id=? AND turn_no=?", (sid, number))
            db.execute("UPDATE turns SET status='sending' WHERE session_id=? AND turn_no=?", (sid, number))
            trigger = db.execute("SELECT source_message_id FROM turn_inputs WHERE session_id=? AND turn_no=? "
                                 "ORDER BY source_message_id DESC LIMIT 1", (sid, number)).fetchone()[0]
        status, error, message_id, retry_at = "unknown", None, None, now
        try:
            async with asyncio.timeout(min(5, max(.001, row["deadline"] - now))):
                sent = await self.bot.send_message(
                    chat_id=chat_id, message_thread_id=row["thread_id"] or None,
                    reply_parameters=ReplyParameters(message_id=trigger, allow_sending_without_reply=False),
                    text=row["text"], parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Стоп", callback_data=callback_data("stop", sid)),
                        InlineKeyboardButton(text=f"Завершить · {number}/4", callback_data=callback_data("finish", sid)),
                    ]] + self.rating_buttons(sid, number)),
                )
                message_id, status = sent.message_id, "sent"
        except TelegramRetryAfter as exc:
            status, error, retry_at = "ready", "429", self.clock() + max(0, exc.retry_after)
        except TelegramForbiddenError:
            status, error = "cancelled", "403"
        except asyncio.CancelledError:
            self._finish_send(sid, number, "unknown", "cancelled_http", None, now)
            raise
        except (TelegramAPIError, OSError, TimeoutError):
            error = "ambiguous_http"
        self._finish_send(sid, number, status, error, message_id, retry_at)
        return True

    def _finish_send(self, sid: str, number: int, status: str, error: str | None,
                     message_id: int | None, retry_at: float) -> None:
        with connect(self.database) as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE session_id=? AND turn_no=?", (sid, number)).fetchone()
            if status == "ready" and not guard(db, turn, self.clock()):
                status = "cancelled"
            db.execute("UPDATE outbox SET status=?,error_code=?,telegram_message_id=?,next_attempt_at=? "
                       "WHERE session_id=? AND turn_no=?", (status, error, message_id, retry_at, sid, number))
            db.execute("UPDATE turns SET status=? WHERE session_id=? AND turn_no=?", (status, sid, number))
            if status == "sent":
                db.execute("UPDATE sessions SET turn_count=turn_count+1,last_sent_at=? WHERE id=? "
                           "AND turn_count=?", (int(self.clock()), sid, number - 1))
                db.execute("UPDATE sessions SET state='closed',closed_reason='turn_limit',revision=revision+1 "
                           "WHERE id=? AND state='active' AND turn_count>=4", (sid,))
            if status == "unknown":
                db.execute("UPDATE sessions SET state='closed',closed_reason='delivery_unknown',revision=revision+1 "
                           "WHERE id=? AND state='active'", (sid,))
            if error == "403":
                db.execute("UPDATE chats SET enabled=0 WHERE chat_id=(SELECT chat_id FROM sessions WHERE id=?)", (sid,))
                db.execute("UPDATE sessions SET state='closed',closed_reason='forbidden',revision=revision+1 "
                           "WHERE chat_id=(SELECT chat_id FROM sessions WHERE id=?) AND state='active'", (sid,))

    async def run_sender(self) -> None:
        while not self.stopping:
            self.invalidate()
            await self.send_once()
            await asyncio.sleep(.1)

    async def handle_update(self, payload: dict[str, object], *, bot_id: int,
                            bot_username: str | None, owner_ids: frozenset[int]) -> None:
        query = payload.get('callback_query')
        if isinstance(query, dict) and isinstance(query.get('data'), str) and query['data'].startswith('rate:'):
            try:
                _, gid, rating = query['data'].split(':')
                message, actor = query['message'], query['from']
                if not isinstance(message, dict) or not isinstance(actor, dict) or actor.get('is_bot'):
                    return
                chat = message['chat']
                if not isinstance(chat, dict):
                    return
                accepted = await asyncio.to_thread(self.generation_log.rate, int(gid), chat=int(chat['id']),
                    thread=topic_id(message), user=int(actor['id']), message_id=int(message['message_id']), rating=int(rating))
                await self.bot.answer_callback_query(callback_query_id=query['id'],
                    text='Оценка сохранена.' if accepted else 'Эта оценка недоступна.')
            except (KeyError, ValueError, TypeError):
                return
            return
        raw = payload.get("message")
        if isinstance(raw, dict):
            timestamp = raw.get("date")
            # Archived messages still reach ingestion, but never execute commands.
            if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                    or abs(self.clock() - timestamp) > 30):
                return
            text = raw.get("text")
            entities_raw = raw.get("entities")
            entities = tuple(e for e in entities_raw if isinstance(e, dict)) if isinstance(entities_raw, list) else ()
            parsed = parse_command(text, entities, bot_username=bot_username) if isinstance(text, str) else None
            expire_sessions(self.database, now=int(self.clock()))
            # Repeating the same target is still the same round. Only an explicit
            # different target or /roast starts another round.
            reply = raw.get("reply_to_message")
            reply_actor = reply.get("from") if isinstance(reply, dict) else None
            targets_human = isinstance(reply_actor, dict) and reply_actor.get("id") != bot_id
            if (parsed and parsed.name == "roast" and isinstance(text, str) and text.startswith("@")
                    and not re.match(r"^(?:меня|себя|me)(?=\s|$)", parsed.argument, re.IGNORECASE)):
                chat, actor = raw.get("chat"), raw.get("from")
                if isinstance(chat, dict) and isinstance(actor, dict):
                    with connect(self.database) as db:
                        continuing = db.execute("""SELECT * FROM sessions WHERE chat_id=?
                            AND thread_id=? AND (target_id=? OR initiator_id=?) AND state='active'
                            """,
                            (chat.get("id"), topic_id(raw),
                             actor.get("id"), actor.get("id"))).fetchone()
                    explicit_target = None
                    if parsed.target_count or targets_human:
                        explicit_target = resolve_target(self.database, CommandContext(
                            int(chat['id']), actor.get('id'), int(raw['message_id']),
                            topic_id(raw), text, entities,
                            reply_actor['id'] if isinstance(reply_actor, dict) and targets_human else None,
                        ), parsed, bot_user_ids=frozenset({bot_id}))
                    if continuing and ((not parsed.target_count and not targets_human)
                                       or explicit_target == continuing['target_id']):
                        await self.handle_message(payload, bot_username)
                        self.invalidate()
                        return
            busy = self.queue.closed or sum(len(q) for q in self.queue.chats.values()) >= self.queue.capacity
            if parsed and parsed.name == "roast" and (not self.ready or busy):
                chat = raw.get("chat")
                actor = raw.get("from")
                if (isinstance(chat, dict) and type(chat.get("id")) is int
                        and chat.get("type") in {"group", "supergroup"}
                        and isinstance(actor, dict) and type(actor.get("id")) is int
                        and not actor.get("is_bot") and not raw.get("sender_chat")):
                    await self.notify(chat["id"], topic_id(raw),
                                      "Генерация пока недоступна. Попробуйте позже." if not self.ready
                                      else "Сейчас занят. Попробуйте позже.")
                return
        await handle_telegram_update(self.database, self.bot, payload, owner_ids=owner_ids,
                                     bot_id=bot_id, bot_username=bot_username,
                                     settings=self.settings_service.snapshot() if self.settings_service else self.settings)
        await self.handle_message(payload, bot_username)
        self.invalidate()

    async def handle_message(self, payload: dict[str, object], bot_username: str | None) -> None:
        """Continue participant replies to delivered punches or bot mentions."""
        edit = payload.get("edited_message")
        raw = payload.get("message")
        if isinstance(edit, dict):
            chat = edit.get("chat")
            if isinstance(chat, dict):
                with connect(self.database) as db:
                    db.execute("UPDATE sessions SET revision=revision+1 WHERE chat_id=? AND id IN "
                               "(SELECT session_id FROM turn_inputs WHERE source_message_id=?)",
                               (chat.get("id"), edit.get("message_id")))
            self.invalidate()
            return
        if not isinstance(raw, dict) or raw.get("sender_chat"):
            self.invalidate()
            return
        chat, actor, text, mid = raw.get("chat"), raw.get("from"), raw.get("text"), raw.get("message_id")
        if (not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}
                or not isinstance(actor, dict) or actor.get("is_bot") or type(actor.get("id")) is not int
                or not isinstance(text, str) or type(mid) is not int):
            return
        timestamp = raw.get("date")
        if not isinstance(timestamp, (int, float)) or abs(self.clock() - timestamp) > 30:
            return
        thread = topic_id(raw)
        from pozor.privacy import blocked

        with connect(self.database) as db:
            if blocked(db, chat["id"], actor["id"]):
                return
            s = db.execute("SELECT * FROM sessions WHERE chat_id=? AND thread_id=? AND state='active'",
                           (chat.get("id"), thread)).fetchone()
            if s is None:
                reply = raw.get('reply_to_message')
                reply_id = reply.get('message_id') if isinstance(reply, dict) else None
                old = db.execute('''SELECT s.* FROM outbox o JOIN sessions s ON s.id=o.session_id
                    WHERE s.chat_id=? AND s.thread_id=? AND o.telegram_message_id=?
                    AND o.status='sent' AND s.state='closed' AND (s.target_id=? OR s.initiator_id=?)''',
                    (chat['id'], thread, reply_id, actor['id'], actor['id'])).fetchone()
                if old:
                    await self.notify(chat['id'], thread,
                        'Этот раунд завершён. Начни новый: /roast @username или /roast ответом на сообщение участника.')
                return
            label_row = db.execute('SELECT display_name FROM members WHERE chat_id=? AND user_id=?',
                                   (s['chat_id'], s['target_id'])).fetchone()
            target_label = label_row['display_name'] if label_row and label_row['display_name'] else 'Участник'
            if s["trigger_message_id"] == mid:
                entities_raw = raw.get("entities", [])
                entities = tuple(e for e in entities_raw if isinstance(e, dict)) if isinstance(entities_raw, list) else ()
                command = parse_command(text, entities, bot_username=bot_username)
                hint = roast_hint(command, target_label) if command else text
                from pozor.ingestion import capture_reply_context

                selected_quote = capture_reply_context(self.database, raw, s['target_id'])
                if selected_quote:
                    hint = (hint + "\nРеплика участника: " + selected_quote).strip()
                self.accept(s["id"], mid, hint.strip() or ("Привет!" if s["mode"] == "dialogue" else "Дружеская прожарка участника."), first=True, author_id=actor['id'])
                return
            reply = raw.get("reply_to_message")
            reply_id = reply.get("message_id") if isinstance(reply, dict) else None
            direct = db.execute("SELECT 1 FROM outbox WHERE session_id=? AND status='sent' "
                                "AND telegram_message_id=?", (s["id"], reply_id)).fetchone() is not None
        from pozor.commands import _utf16_slice

        entities = raw.get("entities", [])
        mention = bool(bot_username and isinstance(entities, list) and any(
            isinstance(e, dict) and e.get("type") == "mention"
            and type(e.get("offset")) is int and type(e.get("length")) is int
            and (_utf16_slice(text, e["offset"], e["length"]) or "").casefold() == "@" + bot_username.casefold()
            for e in entities
        ))
        if not (direct or mention):
            return
        phrase = text.casefold().strip().strip(".! ,")
        if mention and bot_username:
            phrase = phrase.replace("@" + bot_username.casefold(), "").strip().strip(".! ,")
        stop = actor["id"] == s["target_id"] and phrase in {"стоп", "хватит", "отстань", "не продолжай"}
        finish = direct and actor["id"] == s["initiator_id"] and phrase == "всё, доминация"
        if stop or finish:
            close_session(self.database, session_id=s["id"], chat_id=s["chat_id"], thread_id=s["thread_id"],
                          actor_id=actor["id"])
            self.invalidate()
        elif (actor["id"] in (s["target_id"], s["initiator_id"])
              and not text.startswith("/")):
            entities_raw = raw.get("entities", [])
            entities = tuple(e for e in entities_raw if isinstance(e, dict)) if isinstance(entities_raw, list) else ()
            command = parse_command(text, entities, bot_username=bot_username)
            hint = roast_hint(command, target_label) if command else text
            from pozor.ingestion import capture_reply_context

            quote = capture_reply_context(self.database, raw, s['target_id'])
            if quote:
                hint = (hint + '\nРеплика участника: ' + quote).strip()
            self.accept(s["id"], mid, hint.strip() or 'Продолжи по той же теме.', author_id=actor['id'])

    async def aclose(self) -> None:
        self.stopping = True
        for sid, task in self.jobs.items():
            self.queue.cancel(f"session:{sid}")
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        await self.queue.aclose()


async def health_loop(pipeline: DeliveryPipeline, check: Callable[[float], Awaitable[None]]) -> None:
    while not pipeline.stopping:
        try:
            await check(asyncio.get_running_loop().time() + 3)
            pipeline.ready = True
        except (InferenceError, TimeoutError):
            pipeline.ready = False
        await asyncio.sleep(5)
