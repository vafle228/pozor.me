"""Durable Bot API update ingestion and live message persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pozor.storage import connect

logger = logging.getLogger(__name__)


class UpdateSource(Protocol):
    """Small interface implemented by aiogram's Bot client and offline fakes."""

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        allowed_updates: list[str],
    ) -> Sequence[Any]: ...


class IngestionError(RuntimeError):
    """Raised when a Telegram update cannot be safely persisted."""


class PollingConflictError(RuntimeError):
    """Raised when getUpdates cannot run because a webhook or poller owns the bot."""


class ChatMigrationConflict(RuntimeError):
    """Raised when a chat migration cannot preserve two existing scopes safely."""


def _as_mapping(update: Any) -> Mapping[str, Any]:
    if isinstance(update, Mapping):
        return update
    if hasattr(update, "model_dump"):
        result = update.model_dump(mode="json", exclude_none=True)
        if isinstance(result, Mapping):
            return result
    raise IngestionError("update is not a Bot API mapping")


def _utc_timestamp(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IngestionError("message date is missing or invalid")
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError) as exc:
        raise IngestionError("message date is outside the supported range") from exc


def _message_from_payload(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = payload.get(key)
        if message is not None:
            if not isinstance(message, Mapping):
                raise IngestionError("message payload is malformed")
            return message, key.startswith("edited_")
    return None


def _flatten_text(message: Mapping[str, Any]) -> str | None:
    raw = message.get("text")
    if raw is None:
        raw = message.get("caption")
    if isinstance(raw, str):
        return raw
    return None


def _is_bot_command(message: Mapping[str, Any], text: str) -> bool:
    entities = message.get("entities")
    if entities is None:
        entities = message.get("caption_entities")
    if not isinstance(entities, list):
        return False
    return any(
        isinstance(entity, Mapping)
        and entity.get("type") == "bot_command"
        and entity.get("offset") == 0
        and isinstance(entity.get("length"), int)
        and _entity_text(text, 0, entity["length"]) is not None
        for entity in entities
    )


def _entity_text(text: str, offset: int, length: int) -> str | None:
    """Slice Telegram entity offsets, which are measured in UTF-16 code units."""
    if offset < 0 or length < 1:
        return None
    encoded = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    if end > len(encoded):
        return None
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _mentioned_user_ids(
    connection: sqlite3.Connection,
    message: Mapping[str, Any],
    text: str,
    chat_id: int,
    occurred_at: str,
) -> set[int]:
    entities = message.get("entities")
    if entities is None:
        entities = message.get("caption_entities")
    if not isinstance(entities, list):
        return set()

    user_ids: set[int] = set()
    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        if entity.get("type") == "text_mention":
            user = entity.get("user")
            user_id = user.get("id") if isinstance(user, Mapping) else None
            if isinstance(user_id, int) and not isinstance(user_id, bool):
                user_ids.add(user_id)
            continue
        if entity.get("type") != "mention":
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if isinstance(offset, bool) or not isinstance(offset, int):
            continue
        if isinstance(length, bool) or not isinstance(length, int):
            continue
        username = _entity_text(text, offset, length)
        if username is None or not username.startswith("@"):
            continue
        rows = connection.execute(
            """SELECT DISTINCT user_id FROM aliases
               WHERE chat_id = ? AND username = ? COLLATE NOCASE
                 AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)""",
            (chat_id, username[1:], occurred_at, occurred_at),
        ).fetchall()
        if len(rows) == 1:
            user_ids.add(rows[0]["user_id"])
    return user_ids


def _upsert_subjects(
    connection: sqlite3.Connection,
    message_id: int,
    *,
    chat_id: int,
    message: Mapping[str, Any],
    text: str,
    author_id: int | None,
    occurred_at: str,
) -> set[int]:
    """Rebuild live attribution links and return all affected target IDs."""
    previous = connection.execute(
        "SELECT target_id FROM message_subjects WHERE message_id = ?", (message_id,)
    ).fetchall()
    affected = {row["target_id"] for row in previous}
    connection.execute("DELETE FROM message_subjects WHERE message_id = ?", (message_id,))

    relations: set[tuple[int, str, str]] = set()
    if author_id is not None:
        relations.add((author_id, "self", "observed_self"))
    for target_id in _mentioned_user_ids(connection, message, text, chat_id, occurred_at):
        if target_id != author_id:
            relations.add((target_id, "mention", "third_party"))

    reply = message.get("reply_to_message")
    if isinstance(reply, Mapping):
        reply_chat = reply.get("chat")
        parent_chat_id = reply_chat.get("id") if isinstance(reply_chat, Mapping) else chat_id
        parent_id = reply.get("message_id")
        if parent_chat_id == chat_id and isinstance(parent_id, int) and not isinstance(parent_id, bool):
            parent = connection.execute(
                """SELECT author_id FROM messages
                   WHERE scope_chat_id = ? AND source_namespace = ? AND source_message_id = ?""",
                (chat_id, f"live:{chat_id}", parent_id),
            ).fetchone()
            if parent is not None and parent["author_id"] is not None and parent["author_id"] != author_id:
                relations.add((parent["author_id"], "reply", "third_party"))

    for target_id, relation, attribution in relations:
        connection.execute(
            """INSERT INTO message_subjects(message_id, target_id, relation, attribution)
               VALUES (?, ?, ?, ?)""",
            (message_id, target_id, relation, attribution),
        )
        affected.add(target_id)
    return affected


def _apply_message(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> None:
    selected = _message_from_payload(payload)
    if selected is None:
        return
    message, is_edit = selected
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or isinstance(chat.get("id"), bool):
        raise IngestionError("message chat is missing or invalid")
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        raise IngestionError("message identifiers are missing or invalid")
    migrated_to = message.get("migrate_to_chat_id")
    if isinstance(migrated_to, int) and not isinstance(migrated_to, bool):
        _migrate_chat_scope(connection, chat_id, migrated_to)
        return
    # Both configured scope and operator/admin enablement are required. Ignore unknown
    # chats without creating rows or retaining their message text.
    allowed = connection.execute(
        "SELECT 1 FROM chats WHERE chat_id = ? AND allowed = 1 AND enabled = 1", (chat_id,)
    ).fetchone()
    if allowed is None:
        return

    text = _flatten_text(message)
    if text is None or not text.strip():
        return
    # Live commands are control-plane input; never archive their command text.
    if _is_bot_command(message, text):
        return
    date = _utc_timestamp(message.get("date"))
    author = message.get("from")
    author_id = author.get("id") if isinstance(author, Mapping) else None
    if isinstance(author_id, bool) or not isinstance(author_id, int):
        author_id = None
    thread_id = message.get("message_thread_id")
    if isinstance(thread_id, bool) or not isinstance(thread_id, int):
        thread_id = None
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    reply = message.get("reply_to_message")
    reply_source_id = reply.get("message_id") if isinstance(reply, Mapping) else None
    if type(reply_source_id) is not int or reply_source_id <= 0:
        reply_source_id = None
    if (isinstance(reply, Mapping) and isinstance(reply.get("chat"), Mapping)
            and reply["chat"].get("id") != chat_id):
        reply_source_id = None
    namespace = f"live:{chat_id}"
    existing = connection.execute(
        "SELECT id, text_hash, occurred_at FROM messages WHERE source_namespace = ? AND source_message_id = ?",
        (namespace, message_id),
    ).fetchone()

    message_row_id: int
    if existing is None:
        connection.execute(
            """INSERT INTO messages
               (scope_chat_id, source_namespace, source_message_id, thread_id, author_id,
                text, text_hash, occurred_at, edited_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'live')""",
            (chat_id, namespace, message_id, thread_id, author_id, text, text_hash, date),
        )
        inserted = connection.execute(
            "SELECT id FROM messages WHERE source_namespace = ? AND source_message_id = ?",
            (namespace, message_id),
        ).fetchone()
        assert inserted is not None
        message_row_id = inserted["id"]
    else:
        message_row_id = existing["id"]

    should_rebuild_subjects = existing is None or is_edit or existing["text_hash"] != text_hash
    if existing is not None and (is_edit or existing["text_hash"] != text_hash):
        # A repeated Bot API update is idempotent. An edit replaces the canonical
        # body and the FTS trigger updates the index in the same transaction.
        edited_at = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            """UPDATE messages
               SET thread_id = ?, author_id = ?, text = ?, text_hash = ?, occurred_at = ?, edited_at = ?
               WHERE id = ?""",
            (thread_id, author_id, text, text_hash, date, edited_at, existing["id"]),
        )
    if should_rebuild_subjects:
        connection.execute("UPDATE messages SET reply_source_id=? WHERE id=?",
                           (reply_source_id, message_row_id))
        affected = _upsert_subjects(
            connection,
            message_row_id,
            chat_id=chat_id,
            message=message,
            text=text,
            author_id=author_id,
            occurred_at=date,
        )
        if existing is not None:
            for target_id in affected:
                connection.execute(
                    """INSERT INTO data_epochs(chat_id, target_id, epoch) VALUES (?, ?, 1)
                       ON CONFLICT(chat_id, target_id) DO UPDATE SET epoch = epoch + 1""",
                    (chat_id, target_id),
                )

    from pozor.privacy import blocked

    if ((author_id is not None and blocked(connection, chat_id, author_id)) or any(
        blocked(connection, chat_id, row[0]) for row in connection.execute(
            "SELECT target_id FROM message_subjects WHERE message_id=?", (message_row_id,)
        ).fetchall()
    )):
        connection.execute("DELETE FROM messages WHERE id=?", (message_row_id,))
        return
    connection.execute(
        """INSERT INTO members(chat_id, user_id, display_name, current_username, last_seen)
           SELECT ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           WHERE ? IS NOT NULL
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
             display_name = excluded.display_name,
             current_username = excluded.current_username,
             last_seen = excluded.last_seen""",
        (
            chat_id,
            author_id,
            " ".join(part for part in (author.get("first_name"), author.get("last_name")) if isinstance(part, str))
            if isinstance(author, Mapping)
            else None,
            author.get("username") if isinstance(author, Mapping) else None,
            author_id,
        ),
    )
    if isinstance(author, Mapping) and isinstance(author_id, int):
        username = author.get("username")
        if isinstance(username, str) and username:
            active = connection.execute(
                "SELECT id, username FROM aliases WHERE chat_id=? AND user_id=? AND valid_to IS NULL",
                (chat_id, author_id),
            ).fetchall()
            for alias in active:
                if alias["username"].casefold() != username.casefold():
                    connection.execute(
                        "UPDATE aliases SET valid_to=? WHERE id=?",
                        (date, alias["id"]),
                    )
            if not any(alias["username"].casefold() == username.casefold() for alias in active):
                connection.execute(
                    "INSERT INTO aliases(chat_id,user_id,username,valid_from,valid_to,source) "
                    "VALUES (?, ?, ?, ?, NULL, 'live')",
                    (chat_id, author_id, username, date),
                )


def _persist_batch(
    database_path: Path,
    bot_id: int,
    updates: Sequence[Any],
) -> int | None:
    serialized: list[tuple[int, str, int | None, int | None, bool]] = []
    for item in updates:
        update_payload = _as_mapping(item)
        update_id = update_payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise IngestionError("update_id is missing or invalid")
        selected = _message_from_payload(update_payload)
        chat_id: int | None = None
        migration_target: int | None = None
        is_control_event = False
        if selected is not None:
            message, _ = selected
            chat = message.get("chat")
            candidate = chat.get("id") if isinstance(chat, Mapping) else None
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                chat_id = candidate
            candidate_target = message.get("migrate_to_chat_id")
            if isinstance(candidate_target, int) and not isinstance(candidate_target, bool):
                migration_target = candidate_target
            text = _flatten_text(message)
            is_control_event = text is not None and _is_bot_command(message, text)
        else:
            callback = update_payload.get("callback_query")
            callback_message = callback.get("message") if isinstance(callback, Mapping) else None
            callback_chat = callback_message.get("chat") if isinstance(callback_message, Mapping) else None
            candidate = callback_chat.get("id") if isinstance(callback_chat, Mapping) else None
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                chat_id = candidate
                is_control_event = True
        serialized.append(
            (
                update_id,
                json.dumps(update_payload, ensure_ascii=False, separators=(",", ":")),
                chat_id,
                migration_target,
                is_control_event,
            )
        )
    if not serialized:
        return None
    serialized.sort(key=lambda entry: entry[0])

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for update_id, payload, chat_id, migration_target, is_control_event in serialized:
            if chat_id is None:
                continue
            source_is_allowed = connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND allowed = 1", (chat_id,)
            ).fetchone() is not None
            source_is_enabled = connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND allowed = 1 AND enabled = 1", (chat_id,)
            ).fetchone() is not None
            target_is_allowed = migration_target is not None and connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND allowed = 1", (migration_target,)
            ).fetchone() is not None
            if not (source_is_allowed and (source_is_enabled or is_control_event)) and not target_is_allowed:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO inbox(update_id, normalized_payload) VALUES (?, ?)",
                (update_id, payload),
            )
        next_offset = serialized[-1][0] + 1
        connection.execute(
            """INSERT INTO poller_state(bot_id, next_offset) VALUES (?, ?)
               ON CONFLICT(bot_id) DO UPDATE SET next_offset = MAX(next_offset, excluded.next_offset),
                 updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
            (bot_id, next_offset),
        )
    return next_offset


def process_pending(database_path: Path, *, limit: int = 100) -> int:
    """Apply pending inbox updates atomically; failures leave the update retryable."""
    if limit < 1:
        raise ValueError("limit must be positive")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "SELECT update_id, normalized_payload FROM inbox WHERE processed_at IS NULL ORDER BY update_id LIMIT ?",
            (limit,),
        ).fetchall()
        for row in pending:
            try:
                payload = json.loads(row["normalized_payload"])
                _apply_message(connection, payload)
            except (json.JSONDecodeError, IngestionError) as exc:
                logger.warning("discarding malformed Telegram update %s: %s", row["update_id"], exc)
            connection.execute(
                """UPDATE inbox SET processed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                     normalized_payload = '' WHERE update_id = ?""",
                (row["update_id"],),
            )
    return len(pending)


def current_offset(database_path: Path, bot_id: int) -> int | None:
    """Return the durable next offset for a bot, if ingestion has started."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT next_offset FROM poller_state WHERE bot_id = ?", (bot_id,)
        ).fetchone()
    return row["next_offset"] if row else None


async def poll_once(
    source: UpdateSource,
    database_path: Path,
    bot_id: int,
    *,
    timeout: int = 30,
    control_handler: Any | None = None,
) -> int:
    """Fetch one Bot API batch, commit it before issuing a future offset."""
    offset = current_offset(database_path, bot_id)
    updates = await source.get_updates(
        offset=offset,
        timeout=timeout,
        allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"],
    )
    _persist_batch(database_path, bot_id, updates)
    if control_handler is not None:
        with connect(database_path) as connection:
            pending = connection.execute(
                "SELECT update_id,normalized_payload FROM inbox WHERE processed_at IS NULL ORDER BY update_id"
            ).fetchall()
        for row in pending:
            # A preceding /forget_me may have purged later payloads from this batch.
            # Do not route the stale in-memory copy after its durable row was removed.
            with connect(database_path) as connection:
                retained = connection.execute(
                    "SELECT 1 FROM inbox WHERE update_id=? AND processed_at IS NULL",
                    (row['update_id'],),
                ).fetchone()
            if retained is None:
                continue
            try:
                payload = json.loads(row["normalized_payload"])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                await control_handler(payload)
    return process_pending(database_path)


async def run_poller(
    source: UpdateSource,
    database_path: Path,
    bot_id: int,
    *,
    retry_delay: float = 1.0,
    max_retry_delay: float = 30.0,
    stop_event: asyncio.Event | None = None,
    control_handler: Any | None = None,
) -> None:
    """Poll forever with bounded exponential retry, without handing work to an LLM."""
    delay = retry_delay
    while stop_event is None or not stop_event.is_set():
        try:
            await poll_once(source, database_path, bot_id, control_handler=control_handler)
            delay = retry_delay
        except asyncio.CancelledError:
            raise
        except (PollingConflictError, ChatMigrationConflict):
            raise
        except Exception:
            logger.exception("Telegram polling failed; retrying in %.1f seconds", delay)
            if stop_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
            delay = min(delay * 2, max_retry_delay)


def sync_chat_allowlist(
    database_path: Path, chat_ids: Sequence[int], *, retention_days: int | None = None
) -> None:
    """Persist configured chat scope without enabling chats in the future admin flow."""
    if retention_days is not None and retention_days < 1:
        raise ValueError("retention_days must be positive")
    normalized = list(chat_ids)
    if any(isinstance(chat_id, bool) or not isinstance(chat_id, int) for chat_id in normalized):
        raise ValueError("chat allowlist must contain integer IDs")
    if len(normalized) != len(set(normalized)):
        raise ValueError("chat allowlist must not contain duplicate IDs")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE chats SET allowed = 0")
        for chat_id in normalized:
            if retention_days is None:
                connection.execute(
                    """INSERT INTO chats(chat_id, allowed, enabled) VALUES (?, 1, 0)
                       ON CONFLICT(chat_id) DO UPDATE SET allowed = 1""",
                    (chat_id,),
                )
            else:
                connection.execute(
                    """INSERT INTO chats(chat_id, allowed, enabled, retention_days)
                       VALUES (?, 1, 0, ?)
                       ON CONFLICT(chat_id) DO UPDATE SET allowed = 1,
                         retention_days = excluded.retention_days""",
                    (chat_id, retention_days),
                )


def _migrate_chat_scope(connection: sqlite3.Connection, old_chat_id: int, new_chat_id: int) -> None:
    """Move all durable identity references in one transaction or leave them untouched."""
    if old_chat_id == new_chat_id:
        return
    old = connection.execute("SELECT 1 FROM chats WHERE chat_id = ?", (old_chat_id,)).fetchone()
    if old is None:
        # Telegram can repeat the service update after the first atomic migration.
        return
    destination = connection.execute(
        "SELECT allowed, enabled FROM chats WHERE chat_id = ?", (new_chat_id,)
    ).fetchone()
    if destination is not None:
        has_data = any(
            connection.execute(query, (new_chat_id,)).fetchone() is not None
            for query in (
                "SELECT 1 FROM members WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM messages WHERE scope_chat_id = ? LIMIT 1",
                "SELECT 1 FROM data_epochs WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM sessions WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM command_cooldowns WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM command_receipts WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM interaction_receipts WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM imports WHERE chat_id = ? LIMIT 1",
                "SELECT 1 FROM source_chats WHERE destination_chat_id = ? LIMIT 1",
            )
        )
        if has_data or destination["enabled"]:
            raise ChatMigrationConflict(
                "chat migration target already contains enabled or durable data; "
                "operator reconciliation required"
            )
        connection.execute("DELETE FROM chats WHERE chat_id = ?", (new_chat_id,))

    connection.execute(
        """INSERT INTO chats(chat_id, allowed, enabled, policy_version, retention_days, created_at)
           SELECT ?, allowed, enabled, policy_version, retention_days, created_at
           FROM chats WHERE chat_id = ?""",
        (new_chat_id, old_chat_id),
    )
    if destination is not None and destination["allowed"]:
        connection.execute("UPDATE chats SET allowed = 1 WHERE chat_id = ?", (new_chat_id,))
    connection.execute(
        """INSERT INTO members(chat_id, user_id, display_name, current_username, last_seen,
                               consent, consent_version, opt_out)
           SELECT ?, user_id, display_name, current_username, last_seen,
                  consent, consent_version, opt_out
           FROM members WHERE chat_id = ?""",
        (new_chat_id, old_chat_id),
    )
    connection.execute("UPDATE aliases SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
    connection.execute(
        "UPDATE data_epochs SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
    )
    connection.execute("UPDATE sessions SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
    connection.execute(
        "UPDATE sessions SET state='closed',closed_reason='chat_migrated',revision=revision+1 "
        "WHERE chat_id=? AND state='active'", (new_chat_id,)
    )
    connection.execute("UPDATE interaction_receipts SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
    connection.execute(
        "UPDATE command_cooldowns SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
    )
    connection.execute(
        "UPDATE command_receipts SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
    )
    connection.execute(
        "UPDATE messages SET scope_chat_id = ?, source_namespace = ? "
        "WHERE scope_chat_id = ? AND source_namespace = ?",
        (new_chat_id, f"live:{new_chat_id}", old_chat_id, f"live:{old_chat_id}"),
    )
    # Export namespaces identify the source, so only their destination scope moves.
    connection.execute(
        "UPDATE messages SET scope_chat_id=? WHERE scope_chat_id=?", (new_chat_id, old_chat_id)
    )
    connection.execute(
        "UPDATE source_chats SET destination_chat_id=? WHERE destination_chat_id=?",
        (new_chat_id, old_chat_id),
    )
    connection.execute(
        "UPDATE imports SET chat_id=?, status=CASE WHEN status='staged' THEN 'expired' ELSE status END "
        "WHERE chat_id=?", (new_chat_id, old_chat_id),
    )
    connection.execute("UPDATE forgotten_members SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
    connection.execute("DELETE FROM members WHERE chat_id = ?", (old_chat_id,))
    connection.execute("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,))


def cleanup_expired_live_data(
    database_path: Path,
    *,
    now: datetime | None = None,
    inbox_retention: timedelta = timedelta(days=7),
) -> tuple[int, int]:
    """Delete expired live messages and old cleared inbox rows; return deletion counts."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    deleted_messages = 0
    deleted_inbox = 0
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        chats = connection.execute("SELECT chat_id, retention_days FROM chats").fetchall()
        for chat in chats:
            cutoff = (current - timedelta(days=chat["retention_days"])).isoformat(timespec="seconds")
            expired = connection.execute(
                """SELECT m.id, s.target_id FROM messages AS m
                   LEFT JOIN message_subjects AS s ON s.message_id = m.id
                   WHERE m.scope_chat_id = ? AND m.origin = 'live' AND m.occurred_at < ?""",
                (chat["chat_id"], cutoff),
            ).fetchall()
            affected = {row["target_id"] for row in expired if row["target_id"] is not None}
            ids = {row["id"] for row in expired}
            for message_id in ids:
                connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            deleted_messages += len(ids)
            for target_id in affected:
                connection.execute(
                    """INSERT INTO data_epochs(chat_id, target_id, epoch) VALUES (?, ?, 1)
                       ON CONFLICT(chat_id, target_id) DO UPDATE SET epoch = epoch + 1""",
                    (chat["chat_id"], target_id),
                )
        inbox_cutoff = (current - inbox_retention).isoformat(timespec="seconds")
        cursor = connection.execute(
            "DELETE FROM inbox WHERE processed_at IS NOT NULL AND normalized_payload = '' AND received_at < ?",
            (inbox_cutoff,),
        )
        deleted_inbox = cursor.rowcount
    return deleted_messages, deleted_inbox
