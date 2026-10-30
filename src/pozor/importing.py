"""Safe local import adapters and transactional staging for T3.

This module deliberately stops at durable evidence import. It never dispatches
messages through the Telegram command router and never opens referenced files or URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pozor.storage import connect
from reference.core import Alias, ImportRow, select_export

MAX_IMPORT_BYTES = 100 * 1024 * 1024
PARSER_VERSION = "1"
POLICY_VERSION = "1"
STAGING_TTL = timedelta(hours=24)
_USER_ID = re.compile(r"user([1-9][0-9]*)\Z")


class ImportErrorSafe(ValueError):
    """An input, mapping, or staging state cannot be safely imported."""


@dataclass(frozen=True)
class Preview:
    batch_id: str
    manifest_path: Path
    manifest_hash: str
    counts: dict[str, int]
    examples: tuple[dict[str, Any], ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImportErrorSafe("cannot read import file") from exc
    if size > MAX_IMPORT_BYTES:
        raise ImportErrorSafe("import file exceeds the 100 MiB limit")
    return _sha256_file(path)


def _read_json(path: Path) -> Any:
    """Read bounded JSON values; large Desktop exports use the streaming adapter."""
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ImportErrorSafe("install the 'imports' extra for files larger than 8 MiB")
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportErrorSafe("invalid UTF-8 JSON import file") from exc


def _json_root(path: Path) -> Any:
    value = _read_json(path)
    if value is None:
        raise ImportErrorSafe("empty JSON import file")
    return value


def _parse_timestamp(raw: object, timezone: str | None) -> int:
    if isinstance(raw, bool):
        raise ImportErrorSafe("invalid message date")
    if isinstance(raw, (str, int)) and re.fullmatch(r"[0-9]+", str(raw)):
        return int(raw)
    if not isinstance(raw, str):
        raise ImportErrorSafe("invalid message date")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ImportErrorSafe("invalid message date") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if timezone is None:
            raise ImportErrorSafe("naive message dates require an explicit timezone")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        except ZoneInfoNotFoundError as exc:
            raise ImportErrorSafe("unknown timezone") from exc
    return int(parsed.timestamp())


def _normalize_desktop_date(message: Mapping[str, Any], timezone: str) -> dict[str, Any]:
    """Convert the adapter's supported timestamp forms to the reference contract."""
    normalized = dict(message)
    if "date_unixtime" in normalized:
        return normalized
    raw_date = normalized.get("date")
    if raw_date is None:
        raise ImportErrorSafe("selected export message has no date")
    normalized["date_unixtime"] = str(_parse_timestamp(raw_date, timezone))
    return normalized


def _range_bounds(from_date: str, until_date: str, timezone: str) -> tuple[int, int]:
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(until_date)
        zone = UTC if timezone in {"UTC", "Etc/UTC", "Z"} else ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ImportErrorSafe("invalid date range or timezone (Windows needs the 'tzdata' package for IANA zones)") from exc
    if start >= end:
        raise ImportErrorSafe("from date must be before until date")
    start_dt = datetime.combine(start, time.min, zone)
    end_dt = datetime.combine(end, time.min, zone)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _desktop_chat(root: Mapping[str, Any], source_chat_id: int) -> tuple[Mapping[str, Any], Sequence[Any]]:
    if not isinstance(root, Mapping):
        raise ImportErrorSafe("unsupported Telegram Desktop export shape")
    chats = root.get("chats")
    if isinstance(chats, Mapping) and isinstance(chats.get("list"), list):
        candidates = [chat for chat in chats["list"] if isinstance(chat, Mapping) and chat.get("id") == source_chat_id]
        if len(candidates) != 1:
            raise ImportErrorSafe("source chat ID must match exactly one exported chat")
        chat = candidates[0]
        messages = chat.get("messages")
        if not isinstance(messages, list):
            raise ImportErrorSafe("selected export chat has no messages list")
        return chat, messages
    if root.get("type") in {"personal_chat", "private_group", "private_supergroup", "saved_messages"}:
        export_id = root.get("id")
        if export_id != source_chat_id:
            raise ImportErrorSafe("source chat ID does not match the single-chat export")
        messages = root.get("messages")
        if not isinstance(messages, list):
            raise ImportErrorSafe("single-chat export has no messages list")
        return root, messages
    raise ImportErrorSafe("unsupported Telegram Desktop export shape")


def _stream_desktop_messages(path: Path, source_chat_id: int) -> Iterable[Any]:
    """Yield single-chat messages incrementally; chats.list selects one chat first."""
    try:
        import ijson  # type: ignore[import-not-found]
    except ImportError:
        raise ImportErrorSafe("install the 'imports' extra for files larger than 8 MiB") from None
    try:
        export_type: object = None
        export_id: object = None
        has_chat_list = False
        with path.open("rb") as source:
            for prefix, event, value in ijson.parse(source):
                if prefix == "type" and event == "string":
                    export_type = value
                elif prefix == "id" and event in {"number", "string"}:
                    export_id = value
                elif prefix == "chats.list" and event == "start_array":
                    has_chat_list = True
                if prefix == "messages" and event == "start_array":
                    break
        if not has_chat_list:
            if export_id != source_chat_id or export_type not in {
                "personal_chat", "private_group", "private_supergroup", "saved_messages"
            }:
                raise ImportErrorSafe("source chat ID does not match the single-chat export")

            def single_messages() -> Iterable[Any]:
                with path.open("rb") as source:
                    yield from ijson.items(source, "messages.item")

            return single_messages()
        with path.open("rb") as source:
            matches = [
                chat for chat in ijson.items(source, "chats.list.item")
                if isinstance(chat, Mapping) and chat.get("id") == source_chat_id
            ]
        if len(matches) != 1 or not isinstance(matches[0].get("messages"), list):
            raise ImportErrorSafe("source chat ID must match exactly one exported chat")
        return cast(Iterable[Any], iter(matches[0]["messages"]))
    except ImportErrorSafe:
        raise
    except Exception as exc:
        raise ImportErrorSafe("invalid or unsupported Telegram Desktop JSON export") from exc


def _alias_snapshot(database_path: Path, chat_id: int, target_id: int) -> list[Alias]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """SELECT a.username, a.valid_from, a.valid_to FROM aliases AS a
               JOIN members AS m ON m.chat_id=a.chat_id AND m.user_id=a.user_id
               WHERE a.chat_id=? AND a.user_id=? AND a.source='approved' AND a.valid_to IS NOT NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM aliases AS other
                   WHERE other.chat_id=a.chat_id AND other.user_id<>a.user_id
                     AND other.username=a.username COLLATE NOCASE
                     AND other.valid_from<COALESCE(a.valid_to,'9999-12-31T23:59:59+00:00')
                     AND COALESCE(other.valid_to,'9999-12-31T23:59:59+00:00')>a.valid_from
                 )""",
            (chat_id, target_id),
        ).fetchall()
    aliases: list[Alias] = []
    for row in rows:
        try:
            start = _parse_timestamp(row["valid_from"], "UTC")
            end = _parse_timestamp(row["valid_to"], "UTC")
        except ImportErrorSafe:
            continue
        aliases.append(Alias(row["username"], start, end))
    return aliases


def _namespace(source_chat_id: int) -> str:
    return f"telegram-desktop:{source_chat_id}"


def _validate_scope(database_path: Path, chat_id: int, target_id: int) -> None:
    with connect(database_path) as connection:
        chat = connection.execute(
            "SELECT allowed FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if chat is None or not chat["allowed"]:
            raise ImportErrorSafe("destination chat is not in CHAT_ALLOWLIST")
        member = connection.execute(
            "SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat_id, target_id)
        ).fetchone()
        # Unknown targets may be staged, but the manifest carries this fact and
        # apply asks for the same explicit operator identity confirmation.
        _ = member


def _prepare_desktop_rows(
    database_path: Path,
    root: Mapping[str, Any] | Iterable[Any],
    *,
    source_chat_id: int,
    chat_id: int,
    target_id: int,
    since: int,
    until: int,
    timezone: str,
    selected_ids: set[int] | None,
    neighbors: int,
) -> list[ImportRow]:
    raw_messages: Iterable[Any]
    if isinstance(root, Mapping):
        _, raw_messages = _desktop_chat(root, source_chat_id)
    else:
        if isinstance(root, Sequence):
            raise ImportErrorSafe("unsupported Telegram Desktop export shape")
        raw_messages = cast(Iterable[Any], root)
    aliases = _alias_snapshot(database_path, chat_id, target_id)
    try:
        normalized_messages = (
            _normalize_desktop_date(message, timezone)
            if isinstance(message, Mapping) and message.get("type") == "message"
            else message
            for message in raw_messages
        )
        return select_export(
            normalized_messages, target_id=target_id, since=since, until=until,
            approved_aliases=aliases, selected_ids=selected_ids, neighbors=neighbors,
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ImportErrorSafe(f"invalid selected export message: {exc}") from exc


def _dossier_items(root: Any, requested_target_id: int) -> list[dict[str, Any]]:
    if not isinstance(root, Mapping) or root.get("schema_version") != 1:
        raise ImportErrorSafe("dossier schema_version must be 1")
    if type(root.get("target_user_id")) is not int or root["target_user_id"] != requested_target_id:
        raise ImportErrorSafe("dossier target_user_id must match --target")
    items = root.get("items")
    if not isinstance(items, list):
        raise ImportErrorSafe("dossier items must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ImportErrorSafe("dossier item must be an object")
        item_id, kind, text = item.get("id"), item.get("kind"), item.get("text")
        label = item.get("source_label")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in seen:
            raise ImportErrorSafe("dossier item IDs must be unique non-empty strings")
        if kind not in {"operator_note", "pasted_quote"} or not isinstance(text, str) or not text.strip():
            raise ImportErrorSafe("invalid dossier item kind or text")
        if not isinstance(label, str) or not label.strip():
            raise ImportErrorSafe("dossier source_label is required")
        occurred = item.get("occurred_at")
        if occurred is not None:
            if not isinstance(occurred, str):
                raise ImportErrorSafe("occurred_at must be an ISO timestamp or null")
            try:
                parsed = datetime.fromisoformat(occurred)
            except ValueError as exc:
                raise ImportErrorSafe("invalid dossier occurred_at") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ImportErrorSafe("dossier occurred_at must include a timezone")
            occurred = parsed.astimezone(UTC).isoformat(timespec="seconds")
        url = item.get("source_url")
        if url is not None and (not isinstance(url, str) or not url.startswith(("https://", "http://"))):
            raise ImportErrorSafe("source_url must be an HTTP(S) string or null")
        result.append({"id": item_id, "kind": kind, "text": text, "source_label": label,
                       "source_url": url, "occurred_at": occurred})
        seen.add(item_id)
    return result


def _row_payload(row: ImportRow) -> dict[str, Any]:
    return asdict(row)


def preview_import(
    database_path: Path,
    *,
    file_path: Path,
    format: str,
    chat_id: int,
    target_id: int,
    manifest_path: Path,
    source_chat_id: int | None = None,
    from_date: str | None = None,
    until_date: str | None = None,
    timezone: str = "UTC",
    selected_ids: set[int] | None = None,
    neighbors: int = 0,
    operator_id: int | None = None,
) -> Preview:
    """Validate one local file and persist a non-active, expiring preview batch."""
    expire_staged_imports(database_path)
    if target_id <= 0 or chat_id == 0:
        raise ImportErrorSafe("chat and target IDs must be valid")
    source_file = file_path.expanduser().resolve()
    file_hash = _file_digest(source_file)
    _validate_scope(database_path, chat_id, target_id)
    root: Any = None
    normalized: list[dict[str, Any]] = []
    source_id = source_chat_id
    if format == "telegram-desktop":
        if source_chat_id is None:
            raise ImportErrorSafe("--source-chat is required for Telegram Desktop exports")
        if from_date is None or until_date is None:
            raise ImportErrorSafe("--from and --until are required for Telegram Desktop exports")
        since, until = _range_bounds(from_date, until_date, timezone)
        export_input: Mapping[str, Any] | Iterable[Any]
        if source_file.stat().st_size > 8 * 1024 * 1024:
            export_input = _stream_desktop_messages(source_file, source_chat_id)
        else:
            root = _json_root(source_file)
            export_input = root
        rows = _prepare_desktop_rows(
            database_path, export_input, source_chat_id=source_chat_id, chat_id=chat_id,
            target_id=target_id, since=since, until=until, timezone=timezone, selected_ids=selected_ids,
            neighbors=neighbors,
        )
        normalized = [_row_payload(row) for row in rows]
        source_namespace = _namespace(source_chat_id)
    elif format == "dossier-v1":
        if source_chat_id is not None:
            raise ImportErrorSafe("--source-chat is not used with dossier-v1")
        root = _json_root(source_file)
        items = _dossier_items(root, target_id)
        # Manual facts have their own source namespace and never enter messages/FTS.
        normalized = items
        source_id = 0
        source_namespace = f"manual:{chat_id}:{target_id}"
    else:
        raise ImportErrorSafe("format must be telegram-desktop or dossier-v1")

    if selected_ids is not None and format != "telegram-desktop":
        raise ImportErrorSafe("--message-ids applies only to Telegram Desktop exports")
    if neighbors not in (0, 1):
        raise ImportErrorSafe("neighbors must be 0 or 1")
    payload_hash = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    batch_id = uuid.uuid4().hex
    created = datetime.now(UTC)
    manifest = {
        "manifest_version": 1,
        "batch_id": batch_id,
        "format": format,
        "file": str(source_file),
        "file_sha256": file_hash,
        "payload_sha256": payload_hash,
        "source_chat_id": source_id,
        "source_namespace": source_namespace,
        "chat_id": chat_id,
        "target_id": target_id,
        "operator_id": operator_id,
        "from": from_date,
        "until": until_date,
        "timezone": timezone,
        "parser_version": PARSER_VERSION,
        "policy_version": POLICY_VERSION,
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": (created + STAGING_TTL).isoformat(timespec="seconds"),
        "messages": normalized if format == "telegram-desktop" else [],
        "manual_items": normalized if format == "dossier-v1" else [],
        "identity_confirmed": True,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = manifest_path.expanduser().resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)

    counts = {name: 0 for name in ("self", "mention", "reply", "context", "unknown", "manual_claim")}
    if format == "telegram-desktop":
        for row in normalized:
            counts[row["relation"]] += 1
    else:
        counts["manual_claim"] = len(normalized)
    examples = tuple(normalized[:5])
    expires = (created + STAGING_TTL).isoformat(timespec="seconds")

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        allowed = connection.execute("SELECT allowed FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if allowed is None or not allowed["allowed"]:
            manifest_path.unlink(missing_ok=True)
            raise ImportErrorSafe("destination chat left CHAT_ALLOWLIST during preview")
        if format == "telegram-desktop":
            connection.execute(
                """INSERT INTO source_chats(namespace, export_chat_id, destination_chat_id, mapping_status)
                   VALUES (?, ?, ?, 'approved') ON CONFLICT(namespace) DO UPDATE SET
                   mapping_status=CASE WHEN source_chats.destination_chat_id=excluded.destination_chat_id
                   THEN 'approved' ELSE 'blocked' END""",
                (source_namespace, source_chat_id, chat_id),
            )
            mapping = connection.execute(
                "SELECT mapping_status, destination_chat_id FROM source_chats WHERE namespace=?",
                (source_namespace,),
            ).fetchone()
            if mapping["mapping_status"] != "approved" or mapping["destination_chat_id"] != chat_id:
                manifest_path.unlink(missing_ok=True)
                raise ImportErrorSafe("source chat is already mapped to a different destination")
        else:
            connection.execute(
                """INSERT INTO source_chats(namespace, export_chat_id, destination_chat_id, mapping_status)
                   VALUES (?, 0, ?, 'approved') ON CONFLICT(namespace) DO UPDATE SET
                   mapping_status=CASE WHEN source_chats.destination_chat_id=excluded.destination_chat_id
                   THEN 'approved' ELSE 'blocked' END""",
                (source_namespace, chat_id),
            )
            mapping = connection.execute(
                "SELECT mapping_status,destination_chat_id FROM source_chats WHERE namespace=?",
                (source_namespace,),
            ).fetchone()
            if mapping["mapping_status"] != "approved" or mapping["destination_chat_id"] != chat_id:
                manifest_path.unlink(missing_ok=True)
                raise ImportErrorSafe("manual source is already mapped to a different destination")
        connection.execute(
            """INSERT INTO imports(batch_id, manifest_hash, status, operator_id, target_id, chat_id,
               source_namespace, source_file, manifest_path, source_hash, expires_at)
               VALUES (?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, manifest_hash, operator_id, target_id, chat_id, source_namespace,
             str(source_file), str(manifest_path), file_hash, expires),
        )
    return Preview(batch_id, manifest_path, manifest_hash, counts, examples)


def _read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportErrorSafe("manifest is unreadable or invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ImportErrorSafe("unsupported manifest")
    return manifest, hashlib.sha256(raw).hexdigest()


def apply_import(database_path: Path, manifest_path: Path, *, now: datetime | None = None) -> tuple[str, int]:
    """Revalidate file, destination, manifest, and mapping before atomic activation."""
    manifest, manifest_hash = _read_manifest(manifest_path)
    if manifest.get("parser_version") != PARSER_VERSION or manifest.get("policy_version") != POLICY_VERSION:
        raise ImportErrorSafe("preview parser/policy version is stale; create a new preview")
    try:
        expires = datetime.fromisoformat(manifest["expires_at"])
        source_file = Path(manifest["file"])
        batch_id = manifest["batch_id"]
        chat_id, target_id = manifest["chat_id"], manifest["target_id"]
        namespace, file_hash = manifest["source_namespace"], manifest["file_sha256"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ImportErrorSafe("manifest is missing required fields") from exc
    if expires.tzinfo is None:
        raise ImportErrorSafe("manifest expiry must include a timezone")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    expire_staged_imports(database_path, now=current)
    if current >= expires:
        raise ImportErrorSafe("staging preview expired; create a new preview")
    if _file_digest(source_file) != file_hash:
        raise ImportErrorSafe("source file changed after preview; create a new preview")
    if not manifest.get("identity_confirmed"):
        raise ImportErrorSafe("target identity must be explicitly confirmed")

    messages = manifest.get("messages")
    manual_items = manifest.get("manual_items")
    if not isinstance(messages, list) or not isinstance(manual_items, list):
        raise ImportErrorSafe("manifest payload is malformed")
    payload = messages if manifest.get("format") == "telegram-desktop" else manual_items
    calculated = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if calculated != manifest.get("payload_sha256"):
        raise ImportErrorSafe("manifest payload hash mismatch")
    _validate_scope(database_path, chat_id, target_id)
    applied = 0
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        staged = connection.execute("SELECT * FROM imports WHERE batch_id=?", (batch_id,)).fetchone()
        if staged is None or staged["manifest_hash"] != manifest_hash:
            raise ImportErrorSafe("manifest is not the original staged preview")
        if staged["status"] == "active":
            return batch_id, 0
        if staged["status"] != "staged" or staged["expires_at"] <= current.astimezone(UTC).isoformat(timespec="seconds"):
            raise ImportErrorSafe("staged preview is expired or no longer applicable")
        scope = connection.execute("SELECT allowed FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if scope is None or not scope["allowed"]:
            raise ImportErrorSafe("destination chat is not in CHAT_ALLOWLIST")
        if manifest["format"] == "telegram-desktop":
            mapping = connection.execute(
                "SELECT mapping_status,destination_chat_id FROM source_chats WHERE namespace=?", (namespace,)
            ).fetchone()
            if mapping is None or mapping["mapping_status"] != "approved" or mapping["destination_chat_id"] != chat_id:
                raise ImportErrorSafe("source mapping is no longer approved")
        expected_source = _sha256_file(source_file)
        if expected_source != staged["source_hash"]:
            raise ImportErrorSafe("staged source hash changed")

        for item in messages:
            if not isinstance(item, dict):
                raise ImportErrorSafe("malformed normalized message")
            mid, author_id, timestamp, text, relation = (
                item.get("message_id"), item.get("author_id"), item.get("timestamp"),
                item.get("text"), item.get("relation"),
            )
            if type(mid) is not int or mid <= 0 or type(timestamp) is not int or not isinstance(text, str):
                raise ImportErrorSafe("malformed normalized message fields")
            if relation not in {"self", "mention", "reply", "context"}:
                raise ImportErrorSafe("invalid imported relation")
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            source_message_id = mid
            existing = connection.execute(
                "SELECT id,text_hash,scope_chat_id,origin FROM messages WHERE source_namespace=? AND source_message_id=?",
                (namespace, source_message_id),
            ).fetchone()
            if existing and existing["text_hash"] != text_hash:
                raise ImportErrorSafe(f"source message {mid} conflicts with previously imported content")
            if existing and (existing["scope_chat_id"] != chat_id or existing["origin"] != "export"):
                raise ImportErrorSafe(f"source message {mid} is mapped to another scope")
            if existing:
                message_db_id = existing["id"]
            else:
                connection.execute(
                    """INSERT INTO messages(scope_chat_id,source_namespace,source_message_id,thread_id,
                       author_id,text,text_hash,occurred_at,origin) VALUES (?,?,?,NULL,?,?,?,?, 'export')""",
                    (chat_id, namespace, source_message_id, author_id, text, text_hash,
                     datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")),
                )
                message_db_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            attribution = "observed_self" if relation == "self" and author_id == target_id else "third_party"
            connection.execute(
                "INSERT OR IGNORE INTO message_subjects(message_id,target_id,relation,attribution) VALUES (?,?,?,?)",
                (message_db_id, target_id, relation, attribution),
            )
            connection.execute(
                "INSERT OR IGNORE INTO import_items(batch_id,message_id,relation,selected) VALUES (?,?,?,1)",
                (batch_id, message_db_id, relation),
            )
            connection.execute(
                """INSERT INTO data_epochs(chat_id,target_id,epoch) VALUES (?, ?, 1)
                   ON CONFLICT(chat_id,target_id) DO UPDATE SET epoch=epoch+1""",
                (chat_id, target_id),
            )
            applied += 1
        for item in manual_items:
            if not isinstance(item, dict):
                raise ImportErrorSafe("malformed dossier item")
            text = item.get("text")
            if not isinstance(text, str):
                raise ImportErrorSafe("malformed dossier text")
            connection.execute(
                """INSERT INTO manual_items(item_id,batch_id,target_id,text,kind,source_label,source_url,
                   occurred_at,text_hash) VALUES (?,?,?,?,?,?,?,?,?)""",
                (f"{batch_id}:{item['id']}", batch_id, target_id, text, item["kind"], item["source_label"],
                 item.get("source_url"), item.get("occurred_at"), hashlib.sha256(text.encode()).hexdigest()),
            )
            applied += 1
        connection.execute("UPDATE imports SET status='active' WHERE batch_id=? AND status='staged'", (batch_id,))
    return batch_id, applied


def rollback_import(database_path: Path, batch_id: str) -> int:
    """Remove one batch's evidence, preserving message rows shared by other batches."""
    removed = 0
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        batch = connection.execute("SELECT * FROM imports WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ImportErrorSafe("import batch does not exist")
        if batch["status"] == "rolled_back":
            return 0
        rows = connection.execute(
            "SELECT message_id,relation FROM import_items WHERE batch_id=?", (batch_id,)
        ).fetchall()
        connection.execute("DELETE FROM manual_items WHERE batch_id=?", (batch_id,))
        connection.execute("DELETE FROM import_items WHERE batch_id=?", (batch_id,))
        for row in rows:
            message_id = row["message_id"]
            still_used = connection.execute(
                "SELECT 1 FROM import_items WHERE message_id=? AND relation=? LIMIT 1",
                (message_id, row["relation"]),
            ).fetchone()
            if still_used is None:
                connection.execute(
                    "DELETE FROM message_subjects WHERE message_id=? AND target_id=? AND relation=?",
                    (message_id, batch["target_id"], row["relation"]),
                )
            if still_used is None:
                shared = connection.execute("SELECT 1 FROM import_items WHERE message_id=? LIMIT 1", (message_id,)).fetchone()
                message = connection.execute(
                    "SELECT id FROM messages WHERE id=?", (message_id,)
                ).fetchone()
                if message and shared is None:
                    connection.execute("DELETE FROM messages WHERE id=?", (message_id,))
                    connection.execute(
                        """INSERT INTO data_epochs(chat_id,target_id,epoch) VALUES (?, ?, 1)
                           ON CONFLICT(chat_id,target_id) DO UPDATE SET epoch=epoch+1""",
                        (batch["chat_id"], batch["target_id"]),
                    )
                    removed += 1
        connection.execute("UPDATE imports SET status='rolled_back' WHERE batch_id=?", (batch_id,))
    return removed


def expire_staged_imports(database_path: Path, *, now: datetime | None = None) -> int:
    """Mark expired staging previews without touching active evidence."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    with connect(database_path) as connection:
        paths = connection.execute(
            "SELECT manifest_path FROM imports WHERE status='staged' AND expires_at<=?",
            (current.astimezone(UTC).isoformat(timespec="seconds"),),
        ).fetchall()
        cursor = connection.execute(
            "UPDATE imports SET status='expired' WHERE status='staged' AND expires_at<=?",
            (current.astimezone(UTC).isoformat(timespec="seconds"),),
        )
        expired = cursor.rowcount
    for row in paths:
        try:
            Path(row["manifest_path"]).unlink(missing_ok=True)
        except OSError:
            # The expired row remains unusable; an operator can remove the local file.
            pass
    return expired
