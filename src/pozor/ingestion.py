"""Durable Bot API update ingestion and live message persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
    # The enabled flag is the explicit local allowlist. Ignore unknown chats without
    # creating rows or retaining their message text.
    allowed = connection.execute("SELECT 1 FROM chats WHERE chat_id = ? AND enabled = 1", (chat_id,)).fetchone()
    if allowed is None:
        return

    text = _flatten_text(message)
    if text is None or not text.strip():
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
    namespace = f"live:{chat_id}"
    existing = connection.execute(
        "SELECT id, text_hash, occurred_at FROM messages WHERE source_namespace = ? AND source_message_id = ?",
        (namespace, message_id),
    ).fetchone()

    if existing is None:
        connection.execute(
            """INSERT INTO messages
               (scope_chat_id, source_namespace, source_message_id, thread_id, author_id,
                text, text_hash, occurred_at, edited_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'live')""",
            (chat_id, namespace, message_id, thread_id, author_id, text, text_hash, date),
        )
    elif is_edit or existing["text_hash"] != text_hash:
        # A repeated Bot API update is idempotent. An edit replaces the canonical
        # body and the FTS trigger updates the index in the same transaction.
        edited_at = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            """UPDATE messages
               SET thread_id = ?, author_id = ?, text = ?, text_hash = ?, occurred_at = ?, edited_at = ?
               WHERE id = ?""",
            (thread_id, author_id, text, text_hash, date, edited_at, existing["id"]),
        )
        affected = connection.execute(
            "SELECT DISTINCT target_id FROM message_subjects WHERE message_id = ?", (existing["id"],)
        ).fetchall()
        for row in affected:
            connection.execute(
                """INSERT INTO data_epochs(chat_id, target_id, epoch) VALUES (?, ?, 1)
                   ON CONFLICT(chat_id, target_id) DO UPDATE SET epoch = epoch + 1""",
                (chat_id, row["target_id"]),
            )

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


def _persist_batch(
    database_path: Path,
    bot_id: int,
    updates: Sequence[Any],
) -> int | None:
    serialized: list[tuple[int, str, int | None]] = []
    for item in updates:
        update_payload = _as_mapping(item)
        update_id = update_payload.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise IngestionError("update_id is missing or invalid")
        selected = _message_from_payload(update_payload)
        chat_id: int | None = None
        if selected is not None:
            message, _ = selected
            chat = message.get("chat")
            candidate = chat.get("id") if isinstance(chat, Mapping) else None
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                chat_id = candidate
        serialized.append(
            (update_id, json.dumps(update_payload, ensure_ascii=False, separators=(",", ":")), chat_id)
        )
    if not serialized:
        return None
    serialized.sort(key=lambda entry: entry[0])

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for update_id, payload, chat_id in serialized:
            if chat_id is None or connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ? AND enabled = 1", (chat_id,)
            ).fetchone() is None:
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
) -> int:
    """Fetch one Bot API batch, commit it before issuing a future offset."""
    offset = current_offset(database_path, bot_id)
    updates = await source.get_updates(
        offset=offset,
        timeout=timeout,
        allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post"],
    )
    _persist_batch(database_path, bot_id, updates)
    return process_pending(database_path)


async def run_poller(
    source: UpdateSource,
    database_path: Path,
    bot_id: int,
    *,
    retry_delay: float = 1.0,
    max_retry_delay: float = 30.0,
) -> None:
    """Poll forever with bounded exponential retry, without handing work to an LLM."""
    delay = retry_delay
    while True:
        try:
            await poll_once(source, database_path, bot_id)
            delay = retry_delay
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram polling failed; retrying in %.1f seconds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_retry_delay)
