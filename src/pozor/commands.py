"""T2 command parsing, identity resolution, consent, and session state.

Telegram/network authorization is supplied by the command adapter; all state
changes below are local SQLite transactions and can be exercised offline.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pozor.storage import connect

_COMMAND = re.compile(r"^/(?P<name>[A-Za-z_]+)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s|$)")


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str
    username_targets: tuple[str, ...]
    numeric_targets: tuple[int, ...] = ()

    @property
    def target_count(self) -> int:
        return len(self.username_targets) + len(self.numeric_targets)


@dataclass(frozen=True)
class CommandContext:
    chat_id: int
    actor_id: int | None
    message_id: int
    thread_id: int
    text: str
    entities: tuple[dict[str, object], ...] = ()
    reply_user_id: int | None = None
    reply_is_bot: bool = False
    sender_is_bot: bool = False
    sender_is_anonymous: bool = False


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    text: str = ""
    session_id: str | None = None


@dataclass(frozen=True)
class SessionClock:
    """Immutable values required to make one deterministic session decision."""

    session_id: str
    chat_id: int
    thread_id: int
    target_id: int
    initiator_id: int
    revision: int
    evidence_epoch: int
    created_at: int
    last_sent_at: int | None
    sent_turns: int
    active: bool = True
    consenting: bool = True


@dataclass(frozen=True)
class IncomingEvent:
    chat_id: int
    thread_id: int
    actor_id: int
    addresses_session: bool
    stop: bool = False
    finish: bool = False
    admin: bool = False


class CommandError(RuntimeError):
    """A user-facing command precondition was not met."""


def session_close_reason(session: SessionClock, now: int) -> str | None:
    """Apply the bounded T2 clock to a session snapshot."""
    if not session.active:
        return "closed"
    if not session.consenting:
        return "opt_out"
    if session.sent_turns >= 4:
        return "turn_limit"
    if now >= session.created_at + 300:
        return "ttl"
    if session.last_sent_at is not None and now >= session.last_sent_at + 90:
        return "silence"
    return None


def decide_incoming(session: SessionClock, event: IncomingEvent, now: int) -> str:
    """Return the deterministic ignore/close/defer/continue transition."""
    if (event.chat_id, event.thread_id) != (session.chat_id, session.thread_id):
        return "ignore"
    reason = session_close_reason(session, now)
    if reason:
        return "close:" + reason
    authorized = event.actor_id in (session.target_id, session.initiator_id) or event.admin
    if authorized and (event.stop or event.finish):
        return "close:explicit"
    if event.actor_id != session.target_id or not event.addresses_session:
        return "ignore"
    if session.last_sent_at is None:
        return "ignore"
    if now < session.last_sent_at + 10:
        return "defer"
    return "continue"


def _utf16_slice(text: str, offset: int, length: int) -> str | None:
    if offset < 0 or length < 1:
        return None
    raw = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    if end > len(raw):
        return None
    try:
        return raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def parse_command(
    text: str,
    entities: tuple[dict[str, object], ...] = (),
    *,
    bot_username: str | None = None,
) -> ParsedCommand | None:
    """Parse a leading bot command and collect entity-backed username targets."""
    match = _COMMAND.match(text)
    mention_call = None
    if match is None and bot_username:
        mention_call = re.match(r"^@" + re.escape(bot_username) + r"\s+попусти(?:\s|$)", text, re.IGNORECASE)
        if mention_call and not any(
            e.get("type") == "mention" and e.get("offset") == 0
            and e.get("length") == len(bot_username) + 1
            for e in entities
        ):
            mention_call = None
    if match is None and mention_call is None:
        return None
    name = match.group("name").casefold() if match else "roast"
    # Do not interpret commands explicitly addressed to a different bot.
    addressed_to = match.group("bot") if match else None
    if addressed_to is not None and (
        bot_username is None or addressed_to.casefold() != bot_username.casefold()
    ):
        return None
    targets: list[str] = []
    numeric_targets: list[int] = []
    for entity in entities:
        if entity.get("type") not in {"mention", "text_mention"}:
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if mention_call and offset == 0:
            continue
        if type(offset) is not int or type(length) is not int:
            continue
        fragment = _utf16_slice(text, offset, length)
        if entity.get("type") == "text_mention":
            user = entity.get("user")
            user_id = user.get("id") if isinstance(user, dict) else None
            if fragment is not None and type(user_id) is int and user_id > 0:
                numeric_targets.append(user_id)
        elif fragment and fragment.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", fragment):
            targets.append(fragment[1:].casefold())
    consumed = match.end() if match else mention_call.end()  # type: ignore[union-attr]
    return ParsedCommand(
        name,
        text[consumed:].strip(),
        tuple(dict.fromkeys(targets)),
        tuple(dict.fromkeys(numeric_targets)),
    )


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


def set_consent(database_path: Path, chat_id: int, user_id: int, consent: bool) -> bool:
    """Set per-chat opt-in; opt-out closes every active session for that target."""
    with connect(database_path) as connection:
        if not _command_allowed(connection, chat_id, require_enabled=True):
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
        if target_id == initiator_id:
            raise CommandError("Нельзя выбрать себя целью")
        member = connection.execute(
            "SELECT consent, opt_out FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, target_id),
        ).fetchone()
        if member is None or not member["consent"] or member["opt_out"]:
            raise CommandError("Участник должен сначала включить участие командой /roast_on")
        if connection.execute(
            "SELECT 1 FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, target_id),
        ).fetchone() is None:
            raise CommandError("Участник не найден в этом чате")
        if connection.execute(
            "SELECT 1 FROM sessions WHERE chat_id=? AND thread_id=? AND state='active'",
            (chat_id, thread_id),
        ).fetchone():
            raise CommandError("В этой теме уже идёт сессия")
        target_cd = connection.execute(
            "SELECT until_at FROM command_cooldowns WHERE chat_id=? AND user_id=? AND kind='target'",
            (chat_id, target_id),
        ).fetchone()
        initiator_cd = connection.execute(
            "SELECT until_at FROM command_cooldowns WHERE chat_id=? AND user_id=? AND kind='initiator'",
            (chat_id, initiator_id),
        ).fetchone()
        if target_cd and timestamp < target_cd["until_at"]:
            raise CommandError("Для участника действует cooldown")
        if initiator_cd and timestamp < initiator_cd["until_at"]:
            raise CommandError("Для инициатора действует cooldown")
        connection.execute(
            """INSERT INTO sessions
               (id, chat_id, thread_id, target_id, initiator_id, state, created_at, trigger_message_id)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (session_id, chat_id, thread_id, target_id, initiator_id, timestamp, trigger_message_id),
        )
        connection.execute(
            """INSERT INTO command_cooldowns VALUES (?, ?, 'target', ?)
               ON CONFLICT(chat_id,user_id,kind) DO UPDATE SET until_at=excluded.until_at""",
            (chat_id, target_id, timestamp + 600),
        )
        connection.execute(
            """INSERT INTO command_cooldowns VALUES (?, ?, 'initiator', ?)
               ON CONFLICT(chat_id,user_id,kind) DO UPDATE SET until_at=excluded.until_at""",
            (chat_id, initiator_id, timestamp + 30),
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


async def handle_telegram_update(
    database_path: Path,
    bot: Bot,
    payload: dict[str, object],
    *,
    owner_ids: frozenset[int],
    bot_id: int,
    bot_username: str | None = None,
) -> None:
    """Handle T2 control commands using the real aiogram Bot API adapter."""
    from aiogram.enums import ChatMemberStatus

    query = payload.get("callback_query")
    if isinstance(query, dict):
        data = query.get("data")
        user = query.get("from")
        message = query.get("message")
        if not isinstance(message, dict) or not isinstance(user, dict):
            return
        chat, user_id = message.get("chat"), user.get("id")
        if not isinstance(chat, dict) or type(chat.get("id")) is not int or type(user_id) is not int:
            return
        action, session_id = callback_action(data if isinstance(data, str) else None), callback_session_id(
            data if isinstance(data, str) else None
        )
        if action is None or session_id is None:
            return
        thread = message.get("message_thread_id")
        closed = close_session(
            database_path, session_id=session_id, chat_id=chat["id"],
            thread_id=thread if type(thread) is int else 0, actor_id=user_id,
        )
        callback_id = query.get("id")
        if isinstance(callback_id, str):
            await bot.answer_callback_query(
                callback_query_id=callback_id,
                text="Сессия завершена." if closed else "Эта кнопка недоступна для вашего аккаунта.",
            )
        return

    raw = payload.get("message")
    if not isinstance(raw, dict):
        return
    chat = raw.get("chat")
    if not isinstance(chat, dict) or type(chat.get("id")) is not int:
        return
    if chat.get("type") not in {"group", "supergroup"}:
        return
    chat_id = chat["id"]
    actor = raw.get("from")
    actor_id = actor.get("id") if isinstance(actor, dict) else None
    if type(actor_id) is not int or (isinstance(actor, dict) and actor.get("is_bot")):
        return
    text = raw.get("text")
    entities_raw = raw.get("entities")
    entities = tuple(e for e in entities_raw if isinstance(e, dict)) if isinstance(entities_raw, list) else ()
    if not isinstance(text, str):
        return
    parsed = parse_command(text, entities, bot_username=bot_username)
    if parsed is None:
        return
    message_id = raw.get("message_id")
    if type(message_id) is not int:
        return
    thread = raw.get("message_thread_id")
    thread_id = thread if type(thread) is int else 0
    reply = raw.get("reply_to_message")
    reply_user = reply.get("from") if isinstance(reply, dict) else None
    reply_user_id = reply_user.get("id") if isinstance(reply_user, dict) else None
    reply_is_bot = bool(reply_user.get("is_bot")) if isinstance(reply_user, dict) else False
    sender_is_anonymous = bool(raw.get("sender_chat"))
    if sender_is_anonymous and parsed.name in {'forget_me', 'roast_on', 'roast_off'}:
        return
    from pozor.privacy import blocked

    with connect(database_path) as connection:
        if blocked(connection, chat_id, actor_id) and parsed.name not in {'forget_me', 'roast_on'}:
            return
    context = CommandContext(
        chat_id, actor_id, message_id, thread_id, text, entities,
        reply_user_id if type(reply_user_id) is int else None, reply_is_bot,
        False, sender_is_anonymous,
    )
    # The command itself is the first observed membership event in many chats.
    display_name = " ".join(
        part for part in (actor.get("first_name"), actor.get("last_name")) if isinstance(part, str)
    ) if isinstance(actor, dict) else ""
    username = actor.get("username") if isinstance(actor, dict) else None
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO members(chat_id,user_id,display_name,current_username,last_seen)
               SELECT ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE EXISTS (SELECT 1 FROM chats WHERE chat_id=? AND allowed=1)
               AND NOT EXISTS (SELECT 1 FROM forgotten_members WHERE chat_id=? AND user_id=?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
                 display_name=excluded.display_name,current_username=excluded.current_username,
                 last_seen=excluded.last_seen""",
            (chat_id, actor_id, display_name or None, username if isinstance(username, str) else None,
             chat_id, chat_id, actor_id),
        )
        if isinstance(username, str) and username and not blocked(connection, chat_id, actor_id):
            current = connection.execute(
                "SELECT id, username FROM aliases WHERE chat_id=? AND user_id=? AND valid_to IS NULL",
                (chat_id, actor_id),
            ).fetchall()
            timestamp = datetime.now(UTC).isoformat(timespec="seconds")
            for alias in current:
                if alias["username"].casefold() != username.casefold():
                    connection.execute("UPDATE aliases SET valid_to=? WHERE id=?", (timestamp, alias["id"]))
            if not any(alias["username"].casefold() == username.casefold() for alias in current):
                connection.execute(
                    """INSERT INTO aliases(chat_id,user_id,username,valid_from,source)
                       SELECT ?, ?, ?, ?, 'live' WHERE EXISTS
                       (SELECT 1 FROM members WHERE chat_id=? AND user_id=?)""",
                    (chat_id, actor_id, username, timestamp, chat_id, actor_id),
                )
    response = ""
    reply_markup: InlineKeyboardMarkup | None = None
    if parsed.name in {"enable_chat", "disable_chat"}:
        authorized = actor_id in owner_ids
        if not authorized and not sender_is_anonymous:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=actor_id)
                authorized = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
            except TelegramAPIError:
                authorized = False
        if not authorized:
            response = "Команду может выполнить только владелец или администратор этой группы."
        else:
            enabled = parsed.name == "enable_chat"
            if set_chat_enabled(database_path, chat_id, enabled):
                response = "Чат включён." if enabled else "Чат выключен; активные сессии завершены."
            else:
                response = "Чат отсутствует в настроенном CHAT_ALLOWLIST."
    elif parsed.name == "help":
        response = (
            "/enable_chat, /disable_chat — включить или выключить группу (администратор);\n"
            "/whois — ответом на сообщение; /roast_on, /roast_off — согласие участника;\n"
            "/roast @username или ответом на сообщение — начать сессию; /stop, /finish — завершить."
        )
        response += "\n/roast_off выключает участие; /forget_me удаляет данные этой группы."
    elif parsed.name == "whois":
        if context.reply_user_id is None or context.reply_is_bot:
            response = "Используйте /whois ответом на сообщение участника."
        else:
            response = whois(database_path, chat_id, context.reply_user_id) or "Участник ещё не наблюдался в этом чате."
    elif parsed.name == "forget_me":
        from pozor.privacy import forget

        with connect(database_path) as connection:
            permitted = _command_allowed(connection, chat_id, require_enabled=False)
        if not sender_is_anonymous and permitted:
            forget(database_path, chat_id, actor_id)
            response = "Данные в этой группе удалены, участие выключено. /roast_on разрешит новый сбор и импорт."
        else:
            return
    elif parsed.name in {"roast_on", "roast_off"}:
        if set_consent(database_path, chat_id, actor_id, parsed.name == "roast_on"):
            if parsed.name == "roast_on":
                with connect(database_path) as connection:
                    connection.execute("DELETE FROM forgotten_members WHERE chat_id=? AND user_id=?",
                                       (chat_id, actor_id))
            response = "Участие включено для этого чата." if parsed.name == "roast_on" else "Участие выключено; активные сессии завершены."
        else:
            response = "Сначала оператор должен включить этот чат."
    elif parsed.name == "roast":
        if parsed.target_count > 1:
            response = "Укажите одного участника."
        else:
            target = resolve_target(database_path, context, parsed, bot_user_ids=frozenset({bot_id}))
            if target is None:
                response = "Укажите одного известного участника username или ответом на его сообщение."
            else:
                try:
                    session_id = start_session(
                        database_path, chat_id=chat_id, thread_id=thread_id,
                        target_id=target, initiator_id=actor_id, trigger_message_id=message_id,
                    )
                    response = f"Сессия начата для user_id={target}. Используйте /stop, чтобы остановить."
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Стоп", callback_data=callback_data("stop", session_id)),
                        InlineKeyboardButton(text="Завершить", callback_data=callback_data("finish", session_id)),
                    ]])
                except CommandError as exc:
                    response = str(exc)
    elif parsed.name in {"stop", "finish"}:
        admin = actor_id in owner_ids
        # Target/initiator authorization is local: stop must not wait for Telegram.
        closed = close_session(
            database_path, session_id=None, chat_id=chat_id, thread_id=thread_id,
            actor_id=actor_id, is_admin=admin,
        )
        if not closed and not admin:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=actor_id)
                admin = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
            except TelegramAPIError:
                admin = False
            if admin:
                closed = close_session(
                    database_path, session_id=None, chat_id=chat_id, thread_id=thread_id,
                    actor_id=actor_id, is_admin=True,
                )
        response = "Сессия завершена." if closed else "Нет доступной вам активной сессии в этой теме."
    else:
        return
    # Do not repeat a command response if the durable inbox replays after a crash.
    with connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO command_receipts(chat_id, message_id, result_text) VALUES (?, ?, ?)",
            (chat_id, message_id, response),
        )
    if cursor.rowcount:
        await bot.send_message(
            chat_id=chat_id, text=response, message_thread_id=thread_id or None,
            reply_markup=reply_markup, parse_mode=None,
        )
