import tempfile
from pathlib import Path

import pytest

from pozor.ingestion import current_offset, poll_once, process_pending
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
            connection.execute("INSERT INTO chats(chat_id, enabled) VALUES (-1001, 1)")
        yield path


def test_poll_persists_updates_and_offset_before_next_fetch(db_path: Path) -> None:
    source = FakeSource([_message_update(100)])

    processed = _run_immediate(poll_once(source, db_path, 999))

    assert processed == 1
    assert current_offset(db_path, 999) == 101
    assert source.calls[0]["offset"] is None
    assert source.calls[0]["allowed_updates"] == [
        "message", "edited_message", "channel_post", "edited_channel_post"
    ]


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
