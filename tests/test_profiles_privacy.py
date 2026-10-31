"""T6 acceptance: only synthetic text, SQLite, fake Bot API and MockTransport."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from pozor.cli import app
from pozor.commands import handle_telegram_update, start_session
from pozor.config import Settings
from pozor.context import retrieve
from pozor.evaluation import benchmark
from pozor.importing import ImportErrorSafe, apply_import, preview_import, rollback_import
from pozor.inference_queue import InferenceQueue
from pozor.ingestion import _apply_message, _migrate_chat_scope
from pozor.llm import LlamaClient
from pozor.privacy import blocked, cleanup, forget
from pozor.profiles import build_once, commit_batch, epoch, show, source_rows
from pozor.storage import connect, migrate


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "pozor.sqlite3"
    migrate(path)
    with connect(path) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,1,1)")
        db.execute(
            "INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1),(-1,43,1),(-2,42,1)"
        )
    return path


def add(
    database,
    mid,
    *,
    text="Играю три аккорда",
    author=42,
    target=42,
    chat=-1,
    attribution="observed_self",
    relation="self",
    seconds=0,
):
    stamp = datetime.now(UTC) + timedelta(seconds=seconds)
    with connect(database) as db:
        db.execute(
            "INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,"
            "author_id,text,text_hash,occurred_at,origin) VALUES (?,?,?,?,?,?,?,?,'live')",
            (
                mid,
                chat,
                f"live:{chat}",
                mid,
                author,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                stamp.isoformat(),
            ),
        )
        db.execute(
            "INSERT INTO message_subjects VALUES (?,?,?,?)", (mid, target, relation, attribution)
        )


def build(database):
    with connect(database) as db:
        rows = [dict(row) for row in source_rows(db, -1, 42)]
        version = epoch(db, -1, 42)
    assert commit_batch(database, -1, 42, version, rows)


@pytest.mark.parametrize("step,kind,count", [(60, "self_quote", 1), (1800, "self_pattern", 3)])
def test_independent_episodes(database, step, kind, count):
    for number in range(1, 4):
        add(database, number, seconds=number * step)
    build(database)
    (card,) = show(database, -1, 42)
    assert (card["kind"], card["episodes"]) == (kind, count)
    assert len(card["source_ids"]) == 3


def test_third_party_never_promoted_and_no_manual_cards(database):
    for number in range(1, 4):
        add(
            database,
            number,
            author=43,
            relation="mention",
            attribution="third_party",
            seconds=number * 2000,
        )
    build(database)
    (card,) = show(database, -1, 42)
    assert card["attribution"] == "third_party"
    assert card["kind"] == "third_party_joke"
    assert not commit_batch(database, -1, 42, 0, [{"id": 999, "text": "invented"}])


@pytest.mark.parametrize("mutation", ["edit", "delete", "epoch"])
def test_source_invalidation(database, mutation):
    add(database, 1)
    build(database)
    with connect(database) as db:
        if mutation == "edit":
            db.execute("UPDATE messages SET text='new',text_hash='changed' WHERE id=1")
        elif mutation == "delete":
            db.execute("DELETE FROM messages WHERE id=1")
        else:
            db.execute("INSERT INTO data_epochs VALUES (-1,42,1)")
    assert show(database, -1, 42) == []


def test_stale_commit_does_not_advance_watermark(database):
    add(database, 1)
    with connect(database) as db:
        rows = [dict(r) for r in source_rows(db, -1, 42)]
        db.execute("UPDATE messages SET text_hash='changed' WHERE id=1")
    assert not commit_batch(database, -1, 42, 0, rows)
    with connect(database) as db:
        assert db.execute("SELECT * FROM profile_watermarks").fetchall() == []


def test_cards_optional_and_original_attribution_preserved(database):
    add(database, 1, author=43, relation="mention", attribution="third_party")
    build(database)
    for enabled in (False, True):
        pack = retrieve(
            database,
            chat_id=-1,
            thread_id=5,
            target_id=42,
            trigger_id=99,
            hint="аккорды",
            target_label="Synthetic",
            cards_enabled=enabled,
        )
        assert pack.evidence[0].attribution == "third_party"
        assert pack.evidence[0].evidence_id == "message:1"
    assert show(database, -2, 42) == []
    assert show(database, -1, 43) == []


def test_forget_purges_shared_text_inbox_turns_fts_staging_and_keeps_other_group(database):
    add(database, 1)
    add(database, 2, author=43, attribution="third_party", relation="mention")
    add(database, 3, chat=-2)
    build(database)
    sid = start_session(
        database, chat_id=-1, thread_id=0, target_id=43, initiator_id=42, trigger_message_id=99
    )
    with connect(database) as db:
        db.execute(
            "INSERT INTO turns(session_id,turn_no,revision,status,text) VALUES (?,1,0,'ready','private')",
            (sid,),
        )
        db.execute("INSERT INTO turn_inputs VALUES (?,1,99,'private')", (sid,))
        db.execute(
            "INSERT INTO outbox(session_id,turn_no,status,next_attempt_at) VALUES (?,1,'ready',0)",
            (sid,),
        )
        db.execute(
            "INSERT INTO inbox(update_id,normalized_payload) VALUES (1,?)",
            (json.dumps({"message": {"chat": {"id": -1}, "text": "private"}}),),
        )
        db.execute(
            "INSERT INTO inbox(update_id,normalized_payload) VALUES (2,?)",
            (json.dumps({"message": {"chat": {"id": -2}, "text": "retained"}}),),
        )
    forget(database, -1, 42)
    with connect(database) as db:
        assert blocked(db, -1, 42) and not blocked(db, -2, 42)
        assert [r["id"] for r in db.execute("SELECT id FROM messages")] == [3]
        assert [
            r[0]
            for r in db.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'аккорда'")
        ] == [3]
        for table in ("turns", "turn_inputs", "outbox", "profile_cards"):
            assert db.execute(f"SELECT * FROM {table}").fetchall() == []
        assert [r[0] for r in db.execute("SELECT update_id FROM inbox")] == [2]
        member = db.execute("SELECT * FROM members WHERE chat_id=-1 AND user_id=42").fetchone()
        assert not member["consent"] and member["opt_out"]


def make_import(database, *, target=42, manifest_name="preview.json"):
    root = database.parent
    file = root / "synthetic.json"
    file.write_text(
        json.dumps(
            {
                "type": "private_group",
                "id": 9,
                "messages": [
                    {
                        "id": 10,
                        "type": "message",
                        "from_id": f"user{target}",
                        "date_unixtime": "1609459200",
                        "text": "Старая экспортная цитата",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = root / "staging" / manifest_name
    return preview_import(
        database,
        file_path=file,
        format="telegram-desktop",
        chat_id=-1,
        target_id=target,
        source_chat_id=9,
        from_date="2021-01-01",
        until_date="2021-01-02",
        manifest_path=manifest,
    )


def test_import_ttl_from_apply_and_rollback_invalidates(database):
    preview = make_import(database)
    now = datetime.now(UTC)
    apply_import(database, preview.manifest_path, now=now)
    build(database)
    assert show(database, -1, 42)[0]["kind"] == "imported_quote"
    cleanup(database, now=now + timedelta(days=89))
    assert show(database, -1, 42)
    cleanup(database, now=now + timedelta(days=91))
    assert show(database, -1, 42) == []
    assert not preview.manifest_path.exists()
    with connect(database) as db:
        assert not db.execute("SELECT * FROM messages").fetchall()


def test_rollback_invalidates_even_when_source_shared(database):
    first = make_import(database)
    apply_import(database, first.manifest_path)
    second = make_import(database, manifest_name="second.json")
    apply_import(database, second.manifest_path)
    build(database)
    rollback_import(database, first.batch_id)
    assert show(database, -1, 42) == []
    with connect(database) as db:
        assert len(db.execute("SELECT * FROM messages").fetchall()) == 1


def test_forget_cleans_staging_and_rejects_new_import(database):
    staged = make_import(database)
    forget(database, -1, 42)
    assert not staged.manifest_path.exists()
    with pytest.raises(ImportErrorSafe, match="forgetting"):
        make_import(database)
    with connect(database) as db:
        assert db.execute("SELECT status FROM imports").fetchone()[0] == "expired"


def test_forget_blocks_live_authored_mentions_and_survives_chat_migration(database):
    forget(database, -1, 42)
    for actor, entities in [
        (42, []),
        (43, [{"type": "text_mention", "offset": 0, "length": 3, "user": {"id": 42}}]),
    ]:
        with connect(database) as db:
            _apply_message(
                db,
                {
                    "message": {
                        "message_id": actor,
                        "date": int(time.time()),
                        "from": {"id": actor},
                        "chat": {"id": -1, "type": "supergroup"},
                        "text": "Имя текст",
                        "entities": entities,
                    }
                },
            )
            assert db.execute("SELECT * FROM messages").fetchall() == []
    with connect(database) as db:
        _migrate_chat_scope(db, -1, -3)
        assert blocked(db, -3, 42)


class Bot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=123)


@pytest.mark.asyncio
async def test_forget_command_is_self_scoped_and_explicit_optin_required(database):
    add(database, 1)
    bot = Bot()
    payload = {
        "message": {
            "message_id": 100,
            "date": int(time.time()),
            "text": "/forget_me",
            "from": {"id": 42, "first_name": "Synthetic"},
            "chat": {"id": -1, "type": "supergroup"},
        }
    }
    kwargs = {"owner_ids": frozenset(), "bot_id": 99}
    await handle_telegram_update(database, bot, payload, **kwargs)
    assert "удалены" in bot.calls[-1]["text"]
    with connect(database) as db:
        assert blocked(db, -1, 42)
    payload["message"].update(text="/roast_on", message_id=101, sender_chat={"id": -1})
    await handle_telegram_update(database, bot, payload, **kwargs)
    with connect(database) as db:
        assert blocked(db, -1, 42)
    payload["message"].pop("sender_chat")
    await handle_telegram_update(database, bot, payload, **kwargs)
    with connect(database) as db:
        assert not blocked(db, -1, 42)


@pytest.mark.asyncio
async def test_background_token_budget_watermark_and_no_new_data(database):
    for number in range(1, 10):
        add(database, number, text=f"Цитата {number}")
    requests = []

    async def server(request):
        requests.append(request.url.path)
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"is_processing": False}])
        data = json.loads(request.content)
        batch = json.loads(data["content"])
        return httpx.Response(200, json={"tokens": [1] * (len(batch) * 300)})

    async with LlamaClient(
        Settings(_env_file=None), transport=httpx.MockTransport(server)
    ) as client:
        queue = InferenceQueue(client.wait_idle)
        try:
            assert not await build_once(database, client, queue, -1, 42)
            assert requests == []
            assert await build_once(database, client, queue, -1, 42, force=True)
            assert len(show(database, -1, 42)) == 5
            with connect(database) as db:
                assert db.execute("SELECT message_id FROM profile_watermarks").fetchone()[0] == 5
            assert await build_once(database, client, queue, -1, 42, force=True)
            assert not await build_once(database, client, queue, -1, 42, force=True)
        finally:
            await queue.aclose()


@pytest.mark.asyncio
async def test_frozen_eval_suite_offline():
    report = await benchmark(Path("tests/fixtures/eval.jsonl"))
    assert len(report["results"]) == 30
    assert report["retrieval_pass"] and report["evidence_coverage"] == 1
    assert report["model_latency_ms"] is None
    assert report["human_review"] == "pending"
    assert sum(r["control_pass"] is True for r in report["results"]) == 5


def test_profile_cli(database, monkeypatch):
    add(database, 1)
    build(database)
    monkeypatch.setenv("DATABASE_PATH", str(database))
    result = CliRunner().invoke(app, ["profile", "show", "--chat", "-1", "--target", "42"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["source_ids"] == [1]


def test_expired_live_source_unavailable_before_cleanup(database):
    from pozor.context import sources_current

    add(database, 1, seconds=-31 * 86400)
    with connect(database) as db:
        assert not sources_current(db, -1, 42, ["message:1"], time.time())
    assert not retrieve(
        database,
        chat_id=-1,
        thread_id=0,
        target_id=42,
        trigger_id=99,
        hint="аккорда",
        target_label="Synthetic",
    ).recent
    cleanup(database)
    with connect(database) as db:
        assert db.execute("SELECT * FROM messages").fetchall() == []


def test_optout_survives_reimport(database):
    from pozor.commands import set_consent

    assert set_consent(database, -1, 42, False)
    staged = make_import(database)
    apply_import(database, staged.manifest_path)
    with connect(database) as db:
        member = db.execute(
            "SELECT consent,opt_out FROM members WHERE chat_id=-1 AND user_id=42"
        ).fetchone()
        assert tuple(member) == (0, 1)


@pytest.mark.asyncio
async def test_forget_discards_in_memory_remainder_of_poll_batch(database):
    from pozor.ingestion import poll_once

    updates = [
        {
            "update_id": n,
            "message": {
                "message_id": n,
                "date": int(time.time()),
                "chat": {"id": -1, "type": "supergroup"},
                "from": {"id": 42},
                "text": text,
            },
        }
        for n, text in [(1, "/forget_me"), (2, "Удалённый хвост batch")]
    ]

    class Source:
        async def get_updates(self, **kwargs):
            return updates

    calls = []

    async def handler(payload):
        calls.append(payload)
        forget(database, -1, 42)

    await poll_once(Source(), database, 99, control_handler=handler)
    assert len(calls) == 1
    with connect(database) as db:
        assert db.execute("SELECT * FROM inbox").fetchall() == []
        assert db.execute("SELECT next_offset FROM poller_state").fetchone()[0] == 3


def test_manual_claim_and_staged_ttl_cleanup(database):
    file = database.parent / "dossier.json"
    file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_user_id": 42,
                "items": [
                    {
                        "id": "synthetic",
                        "kind": "operator_note",
                        "text": "Операторская вводная",
                        "occurred_at": None,
                        "source_label": "synthetic",
                        "source_url": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    preview = preview_import(
        database,
        file_path=file,
        format="dossier-v1",
        chat_id=-1,
        target_id=42,
        manifest_path=database.parent / "staging" / "manual.json",
    )
    apply_import(database, preview.manifest_path)
    staged = make_import(database)
    cleanup(database, now=datetime.now(UTC) + timedelta(hours=25))
    assert not staged.manifest_path.exists()
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 1
    forget(database, -1, 42)
    assert not preview.manifest_path.exists()
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM manual_items").fetchone()[0] == 0
    assert file.exists()  # Operator's original is never silently deleted.
