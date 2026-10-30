import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from pozor.cli import app
from pozor.importing import (
    ImportErrorSafe,
    apply_import,
    expire_staged_imports,
    preview_import,
    rollback_import,
)
from pozor.storage import connect, migrate


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "pozor.sqlite3"
    migrate(path)
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO chats(chat_id, allowed, enabled) VALUES (-1001, 1, 0)"
        )
        connection.execute(
            "INSERT INTO members(chat_id,user_id,display_name) VALUES (-1001, 42, 'Synthetic')"
        )
    return path


def desktop(tmp_path, messages, *, source_id=9001):
    path = tmp_path / f"export-{source_id}.json"
    path.write_text(json.dumps({"type": "private_group", "id": source_id, "messages": messages}), encoding="utf-8")
    return path


def preview(database, tmp_path, file, *, source_id=9001, selected=None, neighbors=0):
    return preview_import(
        database, file_path=file, format="telegram-desktop", source_chat_id=source_id,
        chat_id=-1001, target_id=42, from_date="2026-01-01", until_date="2026-01-02",
        timezone="UTC", selected_ids=selected, neighbors=neighbors,
        manifest_path=tmp_path / f"{file.stem}-{source_id}-{len(list(tmp_path.glob('*.manifest.json')))}.manifest.json",
    )


def test_desktop_selection_classifies_text_reply_and_ignores_forward_and_unknown_author(database, tmp_path):
    file = desktop(tmp_path, [
        {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42",
         "text": [{"type": "plain", "text": "😀 цитата"}, " да"]},
        {"id": 2, "type": "message", "date_unixtime": "1767225660", "from_id": "user7",
         "reply_to_message_id": 1, "text": "ответ target"},
        {"id": 3, "type": "message", "date_unixtime": "1767225720", "from_id": "user7",
         "forwarded_from": {"name": "someone"}, "text": "переслано"},
        {"id": 4, "type": "message", "date_unixtime": "1767225780", "text": "deleted author"},
        {"id": 5, "type": "message", "date_unixtime": "1767225840", "from_id": "user8",
         "reply_to_message_id": 999, "text": "missing parent"},
        {"id": 6, "type": "service", "date_unixtime": "1767225900", "from_id": "user42", "text": "service"},
    ])
    result = preview(database, tmp_path, file)

    assert result.counts["self"] == 1
    assert result.counts["reply"] == 1
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [item["message_id"] for item in manifest["messages"]] == [1, 2]
    assert manifest["messages"][0]["text"] == "😀 цитата да"


def test_source_message_ids_are_namespaced_and_import_is_idempotent(database, tmp_path):
    message = {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "quote"}
    first_file = desktop(tmp_path, [message], source_id=9001)
    second_file = desktop(tmp_path, [message], source_id=9002)
    first = preview(database, tmp_path, first_file, source_id=9001)
    second = preview(database, tmp_path, second_file, source_id=9002)
    assert apply_import(database, first.manifest_path)[1] == 1
    assert apply_import(database, first.manifest_path)[1] == 0
    assert apply_import(database, second.manifest_path)[1] == 1
    with connect(database) as connection:
        rows = connection.execute("SELECT source_namespace,source_message_id FROM messages ORDER BY source_namespace").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("telegram-desktop:9001", 1), ("telegram-desktop:9002", 1)]


def test_changed_file_after_preview_is_rejected(database, tmp_path):
    file = desktop(tmp_path, [{"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "before"}])
    staged = preview(database, tmp_path, file)
    file.write_text(file.read_text(encoding="utf-8").replace("before", "after"), encoding="utf-8")
    with pytest.raises(ImportErrorSafe, match="changed after preview"):
        apply_import(database, staged.manifest_path)


def test_source_id_mismatch_and_bad_shapes_fail_closed(database, tmp_path):
    file = desktop(tmp_path, [], source_id=9001)
    with pytest.raises(ImportErrorSafe, match="does not match"):
        preview(database, tmp_path, file, source_id=9002)
    file.write_text("[broken", encoding="utf-8")
    with pytest.raises(ImportErrorSafe, match="invalid UTF-8 JSON"):
        preview(database, tmp_path, file)
    file.write_text("[]", encoding="utf-8")
    with pytest.raises(ImportErrorSafe, match="unsupported"):
        preview(database, tmp_path, file)


def test_manifest_edit_and_expired_staging_rejected(database, tmp_path):
    file = desktop(tmp_path, [{"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "quote"}])
    staged = preview(database, tmp_path, file)
    raw = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    raw["target_id"] = 43
    staged.manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ImportErrorSafe, match="original staged"):
        apply_import(database, staged.manifest_path)
    with connect(database) as connection:
        connection.execute("UPDATE imports SET expires_at='2020-01-01T00:00:00+00:00'")
    assert expire_staged_imports(database, now=datetime(2026, 1, 2, tzinfo=UTC)) == 1
    assert not staged.manifest_path.exists()


def test_rollback_isolated_to_batch_and_fts_is_clean(database, tmp_path):
    message = {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "uniquequote"}
    file = desktop(tmp_path, [message])
    first = preview(database, tmp_path, file)
    second = preview(database, tmp_path, file)
    apply_import(database, first.manifest_path)
    apply_import(database, second.manifest_path)
    assert rollback_import(database, first.batch_id) == 0
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM messages WHERE text='uniquequote'").fetchone()[0] == 1
    assert rollback_import(database, second.batch_id) == 1
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'uniquequote'").fetchone()[0] == 0


def test_manual_dossier_stays_manual_and_does_not_enter_live_message_fts(database, tmp_path):
    file = tmp_path / "dossier.json"
    file.write_text(json.dumps({"schema_version": 1, "target_user_id": 42, "items": [
        {"id": "note", "kind": "operator_note", "text": "manual claim", "occurred_at": None,
         "source_label": "Synthetic operator note", "source_url": None}
    ]}), encoding="utf-8")
    staged = preview_import(database, file_path=file, format="dossier-v1", chat_id=-1001,
                            target_id=42, manifest_path=tmp_path / "dossier.manifest.json")
    assert staged.counts["manual_claim"] == 1
    assert apply_import(database, staged.manifest_path)[1] == 1
    with connect(database) as connection:
        item = connection.execute("SELECT kind,text FROM manual_items").fetchone()
        assert tuple(item) == ("operator_note", "manual claim")
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sessions WHERE state='active'").fetchone()[0] == 0


def test_selected_ids_intersect_range_and_missing_selected_id_is_not_silently_added(database, tmp_path):
    file = desktop(tmp_path, [
        {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "inside"},
        {"id": 2, "type": "message", "date_unixtime": "1767312000", "from_id": "user42", "text": "outside"},
    ])
    result = preview(database, tmp_path, file, selected={2})
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["messages"] == []


def test_chats_list_adapter_and_approved_alias_with_ambiguous_history(database, tmp_path):
    with connect(database) as connection:
        for user_id in (43,):
            connection.execute(
                "INSERT INTO members(chat_id,user_id,current_username) VALUES (-1001,?,?)",
                (user_id, f"user{user_id}"),
            )
        connection.execute(
            "INSERT INTO aliases(chat_id,user_id,username,valid_from,valid_to,source) VALUES (-1001,42,'old_name','2025-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','approved')"
        )
        connection.execute(
            "INSERT INTO aliases(chat_id,user_id,username,valid_from,valid_to,source) VALUES (-1001,43,'old_name','2025-06-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','approved')"
        )
    file = tmp_path / "list.json"
    file.write_text(json.dumps({"chats": {"list": [{"id": 9001, "name": "synthetic", "messages": [
        {"id": 11, "type": "message", "date": "2026-01-01T12:00:00Z", "from_id": "user8",
         "text": [{"type": "mention", "text": "@old_name"}, " mention"]},
        {"id": 12, "type": "message", "date": "2026-01-01T12:00:00Z", "from_id": "user8",
         "text": [{"type": "mention", "text": "@safe_name"}, " mention"]},
    ]} ]}}), encoding="utf-8")
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO aliases(chat_id,user_id,username,valid_from,valid_to,source) VALUES (-1001,42,'safe_name','2025-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','approved')"
        )
    staged = preview(database, tmp_path, file)
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    assert [item["message_id"] for item in manifest["messages"]] == [12]
    assert manifest["messages"][0]["relation"] == "mention"


def test_conflict_rolls_back_all_rows_in_batch(database, tmp_path):
    original = desktop(tmp_path, [
        {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "first"},
    ])
    first = preview(database, tmp_path, original)
    apply_import(database, first.manifest_path)
    changed = desktop(tmp_path, [
        {"id": 2, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "new row"},
        {"id": 1, "type": "message", "date_unixtime": "1767225600", "from_id": "user42", "text": "different text"},
    ])
    second = preview(database, tmp_path, changed)
    with pytest.raises(ImportErrorSafe, match="conflicts"):
        apply_import(database, second.manifest_path)
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_cli_synthetic_dry_run_apply_and_fts_query(tmp_path, monkeypatch):
    database = tmp_path / "cli.sqlite3"
    migrate(database)
    with connect(database) as connection:
        connection.execute("INSERT INTO chats(chat_id,allowed) VALUES (-1001,1)")
    fixture = desktop(tmp_path, [{
        "id": 77, "type": "message", "date_unixtime": "1767225600", "from_id": "user42",
        "text": "synthetic acceptance quote",
    }])
    manifest = tmp_path / "cli.manifest.json"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    runner = CliRunner()
    preview_result = runner.invoke(app, [
        "import", "preview", "--file", str(fixture), "--format", "telegram-desktop",
        "--source-chat", "9001", "--chat", "-1001", "--target", "42",
        "--from", "2026-01-01", "--until", "2026-01-02", "--timezone", "UTC",
        "--manifest", str(manifest),
    ])
    assert preview_result.exit_code == 0, preview_result.output
    assert "self': 1" in preview_result.output
    apply_result = runner.invoke(app, ["import", "apply", "--manifest", str(manifest)])
    assert apply_result.exit_code == 0, apply_result.output
    with connect(database) as connection:
        found = connection.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'synthetic acceptance quote'"
        ).fetchone()
        assert found is not None
