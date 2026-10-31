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
        assert tuple(connection.execute("SELECT target_id,relation FROM message_subjects").fetchone()) == (42, "self")
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
    file.write_text(json.dumps({"chats": {"list": [{"id": 9001, "type": "private_group", "name": "synthetic", "messages": [
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
        connection.execute("INSERT INTO members(chat_id,user_id) VALUES (-1001,42)")
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


def quote(author=42, **changes):
    return {"id": 1, "type": "message", "date_unixtime": "1767225600",
            "from_id": f"user{author}", "text": "uniquequote", **changes}


def target_preview(database, tmp_path, file, target):
    return preview_import(
        database, file_path=file, format="telegram-desktop", source_chat_id=9001,
        chat_id=-1001, target_id=target, from_date="2026-01-01", until_date="2026-01-02",
        manifest_path=tmp_path / f"target-{target}.manifest.json",
    )


def epoch(database, target=42):
    with connect(database) as connection:
        row = connection.execute(
            "SELECT epoch FROM data_epochs WHERE chat_id=-1001 AND target_id=?", (target,)
        ).fetchone()
        return row[0] if row else 0


def dossier_preview(database, tmp_path):
    file = tmp_path / "manual.json"
    file.write_text(json.dumps({"schema_version": 1, "target_user_id": 42, "items": [
        {"id": "note", "kind": "operator_note", "text": "synthetic note", "source_label": "test"}
    ]}), encoding="utf-8")
    return preview_import(database, file_path=file, format="dossier-v1", chat_id=-1001,
                          target_id=42, manifest_path=tmp_path / "manual.manifest.json")


def test_rollback_shared_mentions_removes_only_its_target_and_invalidates_epoch(database, tmp_path):
    with connect(database) as connection:
        connection.execute("INSERT INTO members(chat_id,user_id) VALUES (-1001,43)")
    file = desktop(tmp_path, [quote(7, text=[
        {"type": "mention_name", "user_id": 42, "text": "A"},
        {"type": "mention_name", "user_id": 43, "text": "B"},
    ])])
    first = target_preview(database, tmp_path, file, 42)
    second = target_preview(database, tmp_path, file, 43)
    apply_import(database, first.manifest_path)
    apply_import(database, second.manifest_path)
    before = epoch(database)
    other_before = epoch(database, 43)
    assert rollback_import(database, first.batch_id) == 0
    assert epoch(database) == before + 1
    assert epoch(database, 43) == other_before
    with connect(database) as connection:
        assert [tuple(r) for r in connection.execute(
            "SELECT target_id,relation FROM message_subjects"
        )] == [(43, "mention")]
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert rollback_import(database, first.batch_id) == 0
    assert epoch(database) == before + 1
    assert rollback_import(database, second.batch_id) == 1


def test_manual_apply_and_rollback_invalidate_epoch_once(database, tmp_path):
    staged = dossier_preview(database, tmp_path)
    before = epoch(database)
    apply_import(database, staged.manifest_path)
    assert epoch(database) == before + 1
    apply_import(database, staged.manifest_path)
    assert epoch(database) == before + 1
    rollback_import(database, staged.batch_id)
    assert epoch(database) == before + 2
    rollback_import(database, staged.batch_id)
    assert epoch(database) == before + 2
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 0


@pytest.mark.parametrize("change", [{"from_id": "user43"}, {"date_unixtime": "1767225601"}])
def test_duplicate_source_rejects_changed_author_or_date_atomically(database, tmp_path, change):
    file = desktop(tmp_path, [quote()])
    first = preview(database, tmp_path, file)
    apply_import(database, first.manifest_path)
    file = desktop(tmp_path, [quote(id=2, date_unixtime="1767225599"), quote(**change)])
    # Keep the changed author selectable through an explicit mention.
    if "from_id" in change:
        data = json.loads(file.read_text(encoding="utf-8"))
        data["messages"][1]["text_entities"] = [
            {"type": "mention_name", "user_id": 42, "text": "uniquequote"}
        ]
        file.write_text(json.dumps(data), encoding="utf-8")
    staged = preview(database, tmp_path, file)
    before = epoch(database)
    with pytest.raises(ImportErrorSafe, match="conflicts"):
        apply_import(database, staged.manifest_path)
    assert epoch(database) == before
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM imports WHERE batch_id=?",
                                  (staged.batch_id,)).fetchone()[0] == "staged"


def test_export_self_is_imported_quote_and_third_party_stays_third_party(database, tmp_path):
    file = desktop(tmp_path, [quote(), quote(7, id=2, reply_to_message_id=1)])
    staged = preview(database, tmp_path, file)
    apply_import(database, staged.manifest_path)
    with connect(database) as connection:
        assert [tuple(r) for r in connection.execute(
            "SELECT relation,attribution FROM message_subjects ORDER BY message_id"
        )] == [("self", "imported_quote"), ("reply", "third_party")]


@pytest.mark.parametrize("chat_type", ["personal_chat", "saved_messages", "public_channel", None])
@pytest.mark.parametrize("combined", [False, True])
def test_non_group_exports_rejected_in_both_shapes(database, tmp_path, chat_type, combined):
    chat = {"id": 9001, "type": chat_type, "messages": [quote()]}
    file = tmp_path / "rejected.json"
    file.write_text(json.dumps({"chats": {"list": [chat]}} if combined else chat), encoding="utf-8")
    with pytest.raises(ImportErrorSafe, match="only group exports"):
        preview(database, tmp_path, file)
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM imports").fetchone()[0] == 0


@pytest.mark.parametrize("manual", [False, True])
def test_unknown_identity_stages_then_requires_live_member_in_same_chat(database, tmp_path, manual):
    with connect(database) as connection:
        connection.execute("DELETE FROM members")
        connection.execute("INSERT INTO chats(chat_id,allowed) VALUES (-2002,1)")
        connection.execute("INSERT INTO members(chat_id,user_id) VALUES (-2002,42)")
    staged = dossier_preview(database, tmp_path) if manual else preview(
        database, tmp_path, desktop(tmp_path, [quote()])
    )
    assert json.loads(staged.manifest_path.read_text(encoding="utf-8"))["identity_confirmed"] is False
    with pytest.raises(ImportErrorSafe, match="live observation"):
        apply_import(database, staged.manifest_path)
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 0
        connection.execute("INSERT INTO members(chat_id,user_id) VALUES (-1001,42)")
    assert apply_import(database, staged.manifest_path)[1] == 1


def test_identity_is_rechecked_after_preview(database, tmp_path):
    staged = preview(database, tmp_path, desktop(tmp_path, [quote()]))
    with connect(database) as connection:
        connection.execute("DELETE FROM members")
    with pytest.raises(ImportErrorSafe, match="live observation"):
        apply_import(database, staged.manifest_path)


def test_chat_migration_preserves_imports_manual_fts_and_rollback(database, tmp_path):
    from pozor.ingestion import _migrate_chat_scope

    file = desktop(tmp_path, [quote()])
    first = preview(database, tmp_path, file)
    apply_import(database, first.manifest_path)
    manual = dossier_preview(database, tmp_path)
    apply_import(database, manual.manifest_path)
    staged = preview(database, tmp_path, file)
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate_chat_scope(connection, -1001, -2002)
    with connect(database) as connection:
        assert tuple(connection.execute("SELECT scope_chat_id,source_namespace FROM messages").fetchone()) == (
            -2002, "telegram-desktop:9001"
        )
        assert connection.execute("SELECT count(*) FROM imports WHERE chat_id=-2002").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_chats WHERE destination_chat_id=-2002").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'uniquequote'").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM imports WHERE batch_id=?", (staged.batch_id,)).fetchone()[0] == "expired"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ImportErrorSafe):
        apply_import(database, staged.manifest_path)
    assert rollback_import(database, first.batch_id) == 1
    rollback_import(database, manual.batch_id)
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'uniquequote'").fetchone()[0] == 0


def test_migration_rejects_destination_with_only_staging_data(database, tmp_path):
    from pozor.ingestion import ChatMigrationConflict, _migrate_chat_scope

    staged = preview(database, tmp_path, desktop(tmp_path, [quote()]))
    with connect(database) as connection:
        connection.execute("DELETE FROM members")
        connection.execute("INSERT INTO chats(chat_id,allowed) VALUES (-2002,1)")
    with pytest.raises(ChatMigrationConflict), connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate_chat_scope(connection, -2002, -1001)
    with connect(database) as connection:
        assert connection.execute("SELECT status FROM imports WHERE batch_id=?", (staged.batch_id,)).fetchone()[0] == "staged"
        assert connection.execute("SELECT count(*) FROM chats").fetchone()[0] == 2


@pytest.mark.parametrize("chat_type,accepted", [
    ("personal_chat", False), ("saved_messages", False),
    ("private_group", True), ("public_supergroup", True),
])
@pytest.mark.parametrize("combined", [False, True])
def test_large_export_group_type_gate(database, tmp_path, chat_type, accepted, combined):
    pytest.importorskip("ijson")
    chat = {"id": 9001, "type": chat_type, "messages": [quote()]}
    root = {"chats": {"list": [chat]}} if combined else chat
    root["padding"] = " " * (8 * 1024 * 1024)
    file = tmp_path / "large.json"
    file.write_text(json.dumps(root), encoding="utf-8")
    if accepted:
        staged = preview(database, tmp_path, file)
        assert apply_import(database, staged.manifest_path)[1] == 1
    else:
        with pytest.raises(ImportErrorSafe, match="only group exports"):
            preview(database, tmp_path, file)


def test_provenance_migration_repairs_exports_only_and_invalidates_once(tmp_path):
    from pozor.storage import MIGRATIONS_DIR

    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if file.name < "0005":
            (old_migrations / file.name).write_bytes(file.read_bytes())
    database = tmp_path / "old.sqlite3"
    migrate(database, old_migrations)
    with connect(database) as connection:
        connection.execute("INSERT INTO chats(chat_id,allowed) VALUES (-1001,1)")
        for mid, origin in ((1, "export"), (2, "live")):
            connection.execute(
                """INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,
                   author_id,text,text_hash,occurred_at,origin) VALUES (?,-1001,?,?,42,
                   'quote','hash','2026-01-01T00:00:00+00:00',?)""", (mid, origin, mid, origin)
            )
            connection.execute(
                "INSERT INTO message_subjects VALUES (?,42,'self','observed_self')", (mid,)
            )
        connection.execute("INSERT INTO data_epochs VALUES (-1001,42,5)")
    assert migrate(database) == ["0005_import_provenance.sql", "0006_retrieval.sql", "0007_delivery.sql"]
    with connect(database) as connection:
        assert [r[0] for r in connection.execute(
            "SELECT attribution FROM message_subjects ORDER BY message_id"
        )] == ["imported_quote", "observed_self"]
    assert epoch(database) == 6
    assert migrate(database) == []
    assert epoch(database) == 6


def test_previous_policy_preview_requires_new_preview(database, tmp_path, monkeypatch):
    from pozor import importing

    monkeypatch.setattr(importing, "POLICY_VERSION", "1")
    staged = preview(database, tmp_path, desktop(tmp_path, [quote()]))
    monkeypatch.setattr(importing, "POLICY_VERSION", "2")
    with pytest.raises(ImportErrorSafe, match="policy version is stale"):
        apply_import(database, staged.manifest_path)


def test_t4_retrieves_active_imports_manual_claims_and_namespace_parents(database, tmp_path):
    from pozor.context import retrieve

    rows = [quote(id=1, text="reddit победа"),
            quote(id=2, text="игра ответ") | {"reply_to_message_id": 1}]
    batch = preview(database, tmp_path, desktop(tmp_path, rows))
    apply_import(database, batch.manifest_path)
    # Same source message IDs in another namespace must not resolve a missing parent.
    other = preview(database, tmp_path, desktop(tmp_path, [quote(id=1, text="другой")],
                                               source_id=9002), source_id=9002)
    apply_import(database, other.manifest_path)
    dossier = tmp_path / "manual-t4.json"
    dossier.write_text(json.dumps({"schema_version": 1, "target_user_id": 42, "items": [
        {"id": "claim", "kind": "operator_note", "text": "reddit по вводной",
         "source_label": "synthetic", "source_url": None, "occurred_at": None}
    ]}), encoding="utf-8")
    manual = preview_import(database, file_path=dossier, format="dossier-v1", chat_id=-1001,
                            target_id=42, manifest_path=tmp_path / "manual.manifest.json")
    apply_import(database, manual.manifest_path)
    def read():
        return retrieve(database, chat_id=-1001, thread_id=0, target_id=42,
                        trigger_id=99, hint="reddit", target_label="Synthetic")
    result = read()
    assert any(e.text == "reddit победа" and e.attribution == "imported_quote"
               for e in result.evidence)
    assert any(e.text == "reddit по вводной" and e.attribution == "manual_claim"
               for e in result.evidence)
    assert next(e for e in result.evidence if e.text == "игра ответ").parent_status == "available"
    with connect(database) as db:
        db.execute("DELETE FROM messages WHERE source_namespace='telegram-desktop:9001' "
                   "AND source_message_id=1")
    assert next(e for e in read().evidence if e.text == "игра ответ").parent_status == "missing"
    rollback_import(database, batch.batch_id)
    rollback_import(database, manual.batch_id)
    assert all(e.source_namespace == "telegram-desktop:9002" for e in read().evidence)
