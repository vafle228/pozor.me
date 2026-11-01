"""T2 command parsing, identity resolution, consent, and session state.

Telegram/network authorization is supplied by the command adapter; all state
changes below are local SQLite transactions and can be exercised offline.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pozor.core.commands.parsing import (
    CommandContext,
    CommandError,
    CommandResult,
    IncomingEvent,
    ParsedCommand,
    SessionClock,
    _utf16_slice,
    decide_incoming,
    parse_command,
    roast_hint,
    session_close_reason,
)
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.privacy_repo import blocked


def utc_now() -> int:
    return int(datetime.now(UTC).timestamp())


def _member_user_id(connection: sqlite3.Connection, chat_id: int, username: str, now: int) -> int | None:
    rows = connection.execute(
        """SELECT DISTINCT user_id FROM aliases
           WHERE chat_id = ? AND username = ? COLLATE NOCASE
             AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)""",
        (chat_id, username, datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
         datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")),
    ).fetchall()
    if not rows:
        rows = connection.execute(
            "SELECT user_id FROM members WHERE chat_id = ? AND current_username = ? COLLATE NOCASE",
            (chat_id, username),
        ).fetchall()
    if len(rows) != 1:
        return None
    value = rows[0]["user_id"]
    return value if type(value) is int else None


def resolve_target(
    database_path: Path,
    context: CommandContext,
    parsed: ParsedCommand,
    *,
    bot_user_ids: frozenset[int] = frozenset(),
    now: int | None = None,
) -> int | None:
    """Resolve only numeric reply identity or one observed local username alias."""
    if parsed.target_count == 0 and re.match(r"^(?:меня|себя|me)(?=\s|$)", parsed.argument, re.IGNORECASE):
        return context.actor_id
    if parsed.target_count == 0 and (parsed.conversational or not parsed.argument) and (
        context.reply_user_id is None or context.reply_is_bot
    ):
        return context.actor_id
    if context.reply_is_bot:
        return None
    if parsed.target_count > 1:
        return None
    if context.reply_user_id is not None:
        if context.reply_user_id in bot_user_ids:
            return None
        if parsed.username_targets or parsed.numeric_targets:
            explicit_target = parsed.numeric_targets[0] if parsed.numeric_targets else None
            with connect(database_path) as connection:
                if parsed.username_targets:
                    explicit_target = _member_user_id(
                        connection, context.chat_id, parsed.username_targets[0], now or utc_now()
                    )
            if explicit_target != context.reply_user_id:
                return None
        return context.reply_user_id
    if parsed.numeric_targets:
        target = parsed.numeric_targets[0]
        return None if target in bot_user_ids else target
    if len(parsed.username_targets) != 1:
        return None
    with connect(database_path) as connection:
        resolved_target = _member_user_id(
            connection, context.chat_id, parsed.username_targets[0], now or utc_now()
        )
    return None if resolved_target in bot_user_ids else resolved_target


def _command_allowed(connection: sqlite3.Connection, chat_id: int, *, require_enabled: bool) -> bool:
    query = "SELECT allowed, enabled FROM chats WHERE chat_id = ?"
    row = connection.execute(query, (chat_id,)).fetchone()
    return bool(row and row["allowed"] and (row["enabled"] or not require_enabled))


def set_chat_enabled(database_path: Path, chat_id: int, enabled: bool) -> bool:
    """Change only an already allowlisted chat. Returns False outside the allowlist."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE chats SET enabled = ? WHERE chat_id = ? AND allowed = 1",
            (int(enabled), chat_id),
        )
        if enabled:
            return cursor.rowcount == 1
        if cursor.rowcount:
            connection.execute(
                "UPDATE sessions SET state='closed', closed_reason='chat_disabled', revision=revision+1 "
                "WHERE chat_id=? AND state='active'", (chat_id,)
            )
        return cursor.rowcount == 1


def set_consent(database_path: Path, chat_id: int, user_id: int, consent: bool,
                *, respect_forgetting: bool = False) -> bool:
    """Set per-chat opt-in; opt-out closes every active session for that target."""
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _command_allowed(connection, chat_id, require_enabled=True):
            return False
        if respect_forgetting and blocked(connection, chat_id, user_id):
            return False
        cursor = connection.execute(
            "UPDATE members SET consent=?, opt_out=?, consent_version=consent_version+1 "
            "WHERE chat_id=? AND user_id=? AND (consent<>? OR opt_out<>?)",
            (int(consent), int(not consent), chat_id, user_id, int(consent), int(not consent)),
        )
        if not cursor.rowcount:
            exists = connection.execute(
                "SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)
            ).fetchone()
            return exists is not None
        connection.execute(
            """INSERT INTO data_epochs(chat_id,target_id,epoch) VALUES (?, ?, 1)
               ON CONFLICT(chat_id,target_id) DO UPDATE SET epoch=epoch+1""",
            (chat_id, user_id),
        )
        if not consent:
            connection.execute(
                "UPDATE sessions SET state='closed', closed_reason='opt_out', revision=revision+1 "
                "WHERE chat_id=? AND target_id=? AND state='active'", (chat_id, user_id)
            )
        return True


def whois(database_path: Path, chat_id: int, user_id: int) -> str | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT display_name, current_username FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if row is None:
        return None
    label = row["display_name"] or "Участник"
    username = f" (@{row['current_username']})" if row["current_username"] else ""
    return f"{label}{username}: user_id={user_id}, chat_id={chat_id}"


def start_session(
    database_path: Path,
    *,
    chat_id: int,
    thread_id: int,
    target_id: int,
    initiator_id: int,
    trigger_message_id: int,
    now: int | None = None,
    material_available: bool = True,
    mode: Literal["roast", "dialogue"] = "roast",
    level: int = 2,
    daily_limit: int = 12,
) -> str:
    """Create one bounded active session per topic, enforcing consent and cooldowns."""
    timestamp = utc_now() if now is None else now
    session_id = secrets.token_urlsafe(12)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT id FROM sessions WHERE chat_id=? AND trigger_message_id=?",
            (chat_id, trigger_message_id),
        ).fetchone()
        if previous is not None:
            previous_id = previous["id"]
            if isinstance(previous_id, str):
                return previous_id
            raise CommandError("Существующая сессия повреждена")
        if not _command_allowed(connection, chat_id, require_enabled=True):
            raise CommandError("Чат не включён оператором")
        member = connection.execute(
            "SELECT consent, opt_out,roast_level FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, target_id),
        ).fetchone()
        if member is None or not member["consent"] or member["opt_out"]:
            raise CommandError("Участник должен сначала включить участие командой /roast_on")
        rounds = connection.execute("SELECT coalesce(sum(turn_count),0) FROM sessions "
            "WHERE chat_id=? AND target_id=? AND mode='roast' AND created_at>?",
            (chat_id,target_id,timestamp-86400)).fetchone()[0]
        if mode == 'roast' and rounds >= daily_limit:
            raise CommandError('На сегодня достаточно прожарки этого участника. Возвращайтесь завтра.')
        chat_level = connection.execute('SELECT roast_level FROM chats WHERE chat_id=?',(chat_id,)).fetchone()[0]
        level = min(level,member['roast_level'],chat_level)
        if connection.execute(
            "SELECT 1 FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, target_id),
        ).fetchone() is None:
            raise CommandError("Участник не найден в этом чате")
        target_cd = connection.execute(
            "SELECT until_at FROM command_cooldowns WHERE chat_id=? AND user_id=? AND kind='target'",
            (chat_id, target_id),
        ).fetchone()
        initiator_cd = connection.execute(
            "SELECT until_at FROM command_cooldowns WHERE chat_id=? AND user_id=? AND kind='initiator'",
            (chat_id, initiator_id),
        ).fetchone()
        if target_cd and timestamp < target_cd["until_at"]:
            raise CommandError(f"До следующей прожарки участника осталось {target_cd['until_at'] - timestamp} с.")
        if initiator_cd and timestamp < initiator_cd["until_at"]:
            raise CommandError(f"До следующего запуска осталось {initiator_cd['until_at'] - timestamp} с.")
        if not material_available:
            raise CommandError("Пока нет материала для прожарки. Ответьте /roast на сообщение участника "
                               "или добавьте тему после команды. Для истории чата отключите privacy mode у бота.")
        connection.execute(
            "UPDATE sessions SET state='closed',closed_reason='replaced',revision=revision+1 "
            "WHERE chat_id=? AND thread_id=? AND state='active'",
            (chat_id, thread_id),
        )
        connection.execute(
            """INSERT INTO sessions
               (id, chat_id, thread_id, target_id, initiator_id, state, created_at, trigger_message_id, mode,roast_level)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (session_id, chat_id, thread_id, target_id, initiator_id, timestamp, trigger_message_id, mode,level),
        )
        connection.execute(
            """INSERT INTO command_cooldowns VALUES (?, ?, 'target', ?)
               ON CONFLICT(chat_id,user_id,kind) DO UPDATE SET until_at=excluded.until_at""",
            (chat_id, target_id, timestamp + 10),
        )
        connection.execute(
            """INSERT INTO command_cooldowns VALUES (?, ?, 'initiator', ?)
               ON CONFLICT(chat_id,user_id,kind) DO UPDATE SET until_at=excluded.until_at""",
            (chat_id, initiator_id, timestamp + 10),
        )
    return session_id


def close_session(
    database_path: Path,
    *,
    session_id: str | None,
    chat_id: int,
    thread_id: int,
    actor_id: int | None,
    is_admin: bool = False,
    now: int | None = None,
    reason: str = "explicit",
) -> bool:
    """Close a session only for target/initiator/admin in the matching scope."""
    if actor_id is None:
        return False
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        query = "SELECT * FROM sessions WHERE chat_id=? AND thread_id=? AND state='active'"
        args: tuple[object, ...] = (chat_id, thread_id)
        if session_id:
            query += " AND id=?"
            args += (session_id,)
        rows = connection.execute(query, args).fetchall()
        eligible = [r for r in rows if is_admin or actor_id in (r["target_id"], r["initiator_id"])]
        if len(eligible) != 1:
            return False
        session = eligible[0]
        connection.execute(
            "UPDATE sessions SET state='closed', closed_reason=?, revision=revision+1 WHERE id=? AND state='active'",
            (reason, session["id"]),
        )
        return True


def expire_sessions(database_path: Path, *, now: int | None = None) -> int:
    """Close TTL, silence, consent, and max-turn sessions; return number closed."""
    timestamp = utc_now() if now is None else now
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM sessions WHERE state='active'"
        ).fetchall()
        closed = 0
        for session in rows:
            reason: str | None = None
            if session["turn_count"] >= 4:
                reason = "turn_limit"
            elif timestamp >= session["created_at"] + 300:
                reason = "ttl"
            elif session["last_sent_at"] is not None and timestamp >= session["last_sent_at"] + 90:
                reason = "silence"
            else:
                consent = connection.execute(
                    "SELECT consent, opt_out FROM members WHERE chat_id=? AND user_id=?",
                    (session["chat_id"], session["target_id"]),
                ).fetchone()
                if consent is None or not consent["consent"] or consent["opt_out"]:
                    reason = "opt_out"
            if reason:
                connection.execute(
                    "UPDATE sessions SET state='closed', closed_reason=?, revision=revision+1 WHERE id=?",
                    (reason, session["id"]),
                )
                closed += 1
        return closed


def close_sessions_on_startup(database_path: Path) -> int:
    """Do not resume a live interaction across a process restart."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """UPDATE sessions SET state='closed', closed_reason='restart', revision=revision+1
               WHERE state='active'"""
        )
        return cursor.rowcount


def callback_session_id(data: str | None) -> str | None:
    """Extract the embedded session identifier from the versioned button token."""
    if not data:
        return None
    match = re.fullmatch(r"pozor:v1:(stop|finish):([A-Za-z0-9_-]{8,32})", data)
    return match.group(2) if match else None


def callback_action(data: str | None) -> Literal["stop", "finish"] | None:
    match = re.fullmatch(r"pozor:v1:(stop|finish):[A-Za-z0-9_-]{8,32}", data or "")
    if match is None:
        return None
    return "stop" if match.group(1) == "stop" else "finish"


def callback_data(action: Literal["stop", "finish"], session_id: str) -> str:
    """Build compact callback data that binds the button to one exact session."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", session_id):
        raise ValueError("invalid session identifier")
    return f"pozor:v1:{action}:{session_id}"



__all__ = ['CommandContext', 'CommandError', 'CommandResult', 'IncomingEvent', 'ParsedCommand', 'SessionClock', '_command_allowed', '_member_user_id', '_utf16_slice', 'callback_action', 'callback_data', 'callback_session_id', 'close_session', 'close_sessions_on_startup', 'decide_incoming', 'expire_sessions', 'parse_command', 'resolve_target', 'roast_hint', 'session_close_reason', 'set_chat_enabled', 'set_consent', 'start_session', 'utc_now', 'whois']
