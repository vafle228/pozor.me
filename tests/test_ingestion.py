import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pozor.ingestion import (
    ChatMigrationConflict,
    PollingConflictError,
    _persist_batch,
    cleanup_expired_live_data,
    current_offset,
    poll_once,
    process_pending,
    run_poller,
    sync_chat_allowlist,
)
from pozor.storage import connect, migrate


def _message_update(update_id: int, *, chat_id: int = -1001, message_id: int = 7, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1793400000,
            "message_thread_id": 4,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 42, "first_name": "Alex", "username": "alex"},
            "text": text,
        },
    }


class FakeSource:
    def __init__(self, updates: list[dict]) -> None:
        self.updates = updates
        self.calls: list[dict] = []

    async def get_updates(self, *, offset: int | None, timeout: int, allowed_updates: list[str]):
        self.calls.append({"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates})
        result, self.updates = self.updates, []
        return result


def _run_immediate(awaitable):
    """Run the fake source coroutine without opening Windows event-loop sockets."""
    try:
        awaitable.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("offline fake unexpectedly suspended")


@pytest.fixture
def db_path() -> Path:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        path = Path(directory) / "test.sqlite3"
        migrate(path)
        with connect(path) as connection:
            connection.execute("INSERT INTO chats(chat_id, allowed, enabled) VALUES (-1001, 1, 1)")
        yield path


def test_poll_persists_updates_and_offset_before_next_fetch(db_path: Path) -> None:
    source = FakeSource([_message_update(100)])

    processed = _run_immediate(poll_once(source, db_path, 999))

    assert processed == 1
    assert current_offset(db_path, 999) == 101
    assert source.calls[0]["offset"] is None
    assert source.calls[0]["allowed_updates"] == [
        "message", "edited_message", "channel_post", "edited_channel_post", "callback_query"
    ]


def test_t4_live_reply_link_persists_and_foreign_parent_is_rejected(db_path):
    parent = _message_update(900, message_id=90)
    child = _message_update(901, message_id=91)
    child["message"]["reply_to_message"] = parent["message"]
    _persist_batch(db_path, 999, [parent, child])
    process_pending(db_path)
    with connect(db_path) as db:
        assert db.execute("SELECT reply_source_id FROM messages WHERE source_message_id=91").fetchone()[0] == 90
    foreign = _message_update(902, message_id=92)
    foreign["message"]["reply_to_message"] = _message_update(1, chat_id=-2000)["message"]
    _persist_batch(db_path, 999, [foreign])
    process_pending(db_path)
    with connect(db_path) as db:
        assert db.execute("SELECT reply_source_id FROM messages WHERE source_message_id=92").fetchone()[0] is None


def test_disabled_chat_inbox_accepts_only_control_commands(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET enabled=0 WHERE chat_id=-1001")
    plain = _message_update(201, text="private conversation content")
    command = _message_update(202, text="/enable_chat")
    command["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 12}]
    _persist_batch(db_path, 999, [plain, command])
    with connect(db_path) as connection:
        rows = connection.execute("SELECT update_id FROM inbox ORDER BY update_id").fetchall()
    assert [row["update_id"] for row in rows] == [202]


def test_committed_inbox_update_survives_restart_before_processing(db_path: Path) -> None:
    _persist_batch(db_path, 999, [_message_update(100)])
    assert current_offset(db_path, 999) == 101
    with connect(db_path) as connection:
        assert connection.execute("SELECT processed_at FROM inbox WHERE update_id = 100").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0

    # A restarted process drains the durable inbox without asking Telegram for the update again.
    assert process_pending(db_path) == 1
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT normalized_payload FROM inbox WHERE update_id = 100").fetchone()[0] == ""


def test_duplicate_update_does_not_duplicate_message(db_path: Path) -> None:
    source = FakeSource([_message_update(100), _message_update(100)])
    _run_immediate(poll_once(source, db_path, 999))

    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 1


def test_unknown_chat_is_not_retained(db_path: Path) -> None:
    source = FakeSource([_message_update(100, chat_id=-2002, text="private outsider text")])
    _run_immediate(poll_once(source, db_path, 999))

    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 1
        payload = connection.execute("SELECT normalized_payload FROM inbox").fetchone()
        assert payload is None


def test_edit_replaces_text_and_fts_atomically(db_path: Path) -> None:
    source = FakeSource([_message_update(100, text="old words")])
    _run_immediate(poll_once(source, db_path, 999))
    source = FakeSource([
        {"update_id": 101, "edited_message": _message_update(101, text="new words")["message"]}
    ])
    _run_immediate(poll_once(source, db_path, 999))

    with connect(db_path) as connection:
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "new words"
        assert connection.execute("SELECT normalized_payload FROM inbox WHERE update_id = 101").fetchone()[0] == ""
        assert connection.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'old'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'new'"
        ).fetchone()[0] == 1


def test_live_message_records_self_text_mention_and_unambiguous_alias(db_path: Path) -> None:
    from pozor.ingestion import _persist_batch

    with connect(db_path) as connection:
        connection.execute("INSERT INTO members(chat_id, user_id) VALUES (-1001, 42), (-1001, 77)")
        connection.execute(
            """INSERT INTO aliases(chat_id, user_id, username, valid_from, source)
               VALUES (-1001, 77, 'maxwell', '2026-01-01T00:00:00+00:00', 'observed')"""
        )

    message = _message_update(100, text="🔥 @maxwell")
    message["message"]["entities"] = [{"type": "mention", "offset": 3, "length": 8}]
    _persist_batch(db_path, 999, [message])
    process_pending(db_path)

    with connect(db_path) as connection:
        subjects = connection.execute(
            "SELECT target_id, relation, attribution FROM message_subjects ORDER BY relation"
        ).fetchall()
    assert [tuple(row) for row in subjects] == [
        (77, "mention", "third_party"),
        (42, "self", "observed_self"),
    ]


def test_ambiguous_alias_is_not_attributed_but_text_mention_is(db_path: Path) -> None:
    from pozor.ingestion import _persist_batch

    with connect(db_path) as connection:
        connection.execute("INSERT INTO members(chat_id, user_id) VALUES (-1001, 42), (-1001, 77), (-1001, 88)")
        connection.executemany(
            """INSERT INTO aliases(chat_id, user_id, username, valid_from, source)
               VALUES (-1001, ?, 'maxwell', '2026-01-01T00:00:00+00:00', 'observed')""",
            [(77,), (88,)],
        )

    message = _message_update(100, text="@maxwell")
    message["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 8}]
    message["message"]["entities"].append(
        {"type": "text_mention", "offset": 0, "length": 1, "user": {"id": 91}}
    )
    _persist_batch(db_path, 999, [message])
    process_pending(db_path)

    with connect(db_path) as connection:
        subjects = connection.execute(
            "SELECT target_id, relation FROM message_subjects ORDER BY target_id, relation"
        ).fetchall()
    assert [tuple(row) for row in subjects] == [(42, "self"), (91, "mention")]


def test_reply_links_to_parent_author_and_edit_rebuilds_links(db_path: Path) -> None:
    from pozor.ingestion import _persist_batch

    _persist_batch(db_path, 999, [_message_update(100, message_id=10)])
    process_pending(db_path)
    reply = _message_update(101, message_id=11, text="a reply")
    reply["message"]["from"] = {"id": 77, "first_name": "Sam"}
    reply["message"]["reply_to_message"] = {
        "message_id": 10,
        "chat": {"id": -1001},
        "from": {"id": 42},
    }
    _persist_batch(db_path, 999, [reply])
    process_pending(db_path)

    edited = {"update_id": 102}
    edited["edited_message"] = dict(reply["message"])
    edited["edited_message"]["reply_to_message"] = {
        "message_id": 999,
        "chat": {"id": -1001},
        "from": {"id": 91},
    }
    _persist_batch(db_path, 999, [edited])
    process_pending(db_path)

    with connect(db_path) as connection:
        subjects = connection.execute(
            "SELECT target_id, relation FROM message_subjects ORDER BY target_id, relation"
        ).fetchall()
        epochs = connection.execute(
            "SELECT target_id, epoch FROM data_epochs ORDER BY target_id"
        ).fetchall()
    assert [tuple(row) for row in subjects] == [
        (42, "self"),
        (77, "self"),
    ]
    assert [tuple(row) for row in epochs] == [(42, 1), (77, 1)]


def test_failed_processing_rolls_back_processed_marker_and_message(db_path: Path, monkeypatch) -> None:
    from pozor import ingestion

    ingestion._persist_batch(db_path, 999, [_message_update(100)])

    def fail(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ingestion, "_apply_message", fail)
    with pytest.raises(RuntimeError, match="simulated crash"):
        process_pending(db_path)

    with connect(db_path) as connection:
        assert connection.execute("SELECT processed_at FROM inbox").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_edit_does_not_leak_raw_text_into_logging(db_path: Path, caplog) -> None:
    # The receiver logs errors and update IDs only, never payload contents.
    source = FakeSource([_message_update(100, text="synthetic private text")])
    _run_immediate(poll_once(source, db_path, 999))
    assert "synthetic private text" not in caplog.text


def test_allowlist_sync_never_enables_a_chat_and_rejects_others(db_path: Path) -> None:
    sync_chat_allowlist(db_path, [-1001, -3003])
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET enabled = 1 WHERE chat_id = -1001")
    sync_chat_allowlist(db_path, [-3003])

    with connect(db_path) as connection:
        rows = connection.execute("SELECT chat_id, allowed, enabled FROM chats ORDER BY chat_id").fetchall()
    assert [tuple(row) for row in rows] == [(-3003, 1, 0), (-1001, 0, 1)]

    source = FakeSource([_message_update(100, chat_id=-1001, text="outside allowlist")])
    _run_immediate(poll_once(source, db_path, 999))
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT normalized_payload FROM inbox").fetchone() is None


def test_group_to_supergroup_migration_moves_scope_atomically(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET chat_id = -1 WHERE chat_id = -1001")
        connection.execute("INSERT INTO members(chat_id, user_id) VALUES (-1, 77)")
        connection.execute(
            """INSERT INTO aliases(chat_id, user_id, username, valid_from, source)
               VALUES (-1, 77, 'sam', '2026-01-01T00:00:00Z', 'observed')"""
        )
        connection.execute("INSERT INTO data_epochs(chat_id, target_id, epoch) VALUES (-1, 77, 4)")
        connection.execute(
            """INSERT INTO sessions(id,chat_id,thread_id,target_id,initiator_id,state,created_at,trigger_message_id)
               VALUES ('migration_12345678',-1,3,77,88,'active',100,5)"""
        )
        connection.execute(
            "INSERT INTO command_cooldowns(chat_id,user_id,kind,until_at) VALUES (-1,77,'target',700)"
        )
    _persist_batch(db_path, 999, [_message_update(100, chat_id=-1, message_id=10)])
    process_pending(db_path)
    transition = _message_update(101, chat_id=-1, message_id=11)
    transition["message"]["migrate_to_chat_id"] = -1001
    _persist_batch(db_path, 999, [transition])
    process_pending(db_path)

    with connect(db_path) as connection:
        chats = connection.execute("SELECT chat_id, allowed, enabled FROM chats").fetchall()
        messages = connection.execute(
            "SELECT scope_chat_id, source_namespace, source_message_id FROM messages"
        ).fetchall()
        member = connection.execute("SELECT chat_id, user_id FROM members WHERE user_id = 77").fetchone()
        alias = connection.execute("SELECT chat_id, user_id FROM aliases WHERE username = 'sam'").fetchone()
        epoch = connection.execute(
            "SELECT chat_id, target_id, epoch FROM data_epochs WHERE target_id = 77"
        ).fetchone()
        session = connection.execute("SELECT chat_id,thread_id FROM sessions WHERE id='migration_12345678'").fetchone()
        cooldown = connection.execute("SELECT chat_id,user_id FROM command_cooldowns").fetchone()
    assert [tuple(row) for row in chats] == [(-1001, 1, 1)]
    assert [tuple(row) for row in messages] == [(-1001, "live:-1001", 10)]
    assert tuple(member) == (-1001, 77)
    assert tuple(alias) == (-1001, 77)
    assert tuple(epoch) == (-1001, 77, 4)
    assert tuple(session) == (-1001, 3)
    assert tuple(cooldown) == (-1001, 77)


def test_migration_merges_configured_empty_destination_placeholder(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET chat_id = -1 WHERE chat_id = -1001")
    sync_chat_allowlist(db_path, [-1001])
    transition = _message_update(100, chat_id=-1, message_id=11)
    transition["message"]["migrate_to_chat_id"] = -1001
    _persist_batch(db_path, 999, [transition])
    process_pending(db_path)

    with connect(db_path) as connection:
        chat = connection.execute("SELECT chat_id, allowed, enabled FROM chats").fetchone()
    assert tuple(chat) == (-1001, 1, 1)


def test_chat_migration_collision_rolls_back_inbox_and_scope(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute("INSERT INTO chats(chat_id, allowed, enabled) VALUES (-2002, 1, 1)")
    transition = _message_update(100, chat_id=-1001)
    transition["message"]["migrate_to_chat_id"] = -2002
    _persist_batch(db_path, 999, [transition])
    with pytest.raises(ChatMigrationConflict, match="operator reconciliation"):
        process_pending(db_path)

    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chats WHERE chat_id = -1001").fetchone()[0] == 1
        assert connection.execute("SELECT processed_at FROM inbox").fetchone()[0] is None


def test_cleanup_deletes_expired_live_rows_and_invalidates_epochs(db_path: Path) -> None:
    old = _message_update(100, message_id=50, text="expired text")
    old["message"]["date"] = 1
    _persist_batch(db_path, 999, [old])
    process_pending(db_path)
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE inbox SET received_at = '2020-01-01T00:00:00Z' WHERE update_id = 100"
        )

    deleted_messages, deleted_inbox = cleanup_expired_live_data(
        db_path, now=datetime(2026, 10, 31, tzinfo=UTC)
    )
    assert (deleted_messages, deleted_inbox) == (1, 1)
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0
        assert tuple(connection.execute("SELECT target_id, epoch FROM data_epochs").fetchone()) == (42, 1)


def test_polling_conflict_is_actionable_and_not_retried(db_path: Path) -> None:
    class ConflictSource:
        calls = 0

        async def get_updates(self, *, offset, timeout, allowed_updates):
            self.calls += 1
            raise PollingConflictError("active webhook")

    source = ConflictSource()
    with pytest.raises(PollingConflictError, match="active webhook"):
        _run_immediate(run_poller(source, db_path, 999))
    assert source.calls == 1


def test_retry_backoff_is_bounded_and_stoppable(db_path: Path, monkeypatch) -> None:
    from pozor import ingestion

    delays: list[float] = []
    stop = asyncio.Event()
    original_wait_for = asyncio.wait_for

    async def capture_wait_for(awaitable, *, timeout):
        delays.append(timeout)
        if len(delays) < 3:
            awaitable.close()
            raise TimeoutError
        return await original_wait_for(awaitable, timeout=timeout)

    class FlakySource:
        calls = 0

        async def get_updates(self, *, offset, timeout, allowed_updates):
            self.calls += 1
            if self.calls < 3:
                raise OSError("offline synthetic source")
            stop.set()
            return []

    monkeypatch.setattr(ingestion.asyncio, "wait_for", capture_wait_for)
    source = FlakySource()
    _run_immediate(run_poller(source, db_path, 999, retry_delay=0.5, max_retry_delay=0.75, stop_event=stop))
    assert delays == [0.5, 0.75]
    assert source.calls == 3
