"""Offline end-to-end import/model/review tests and account/HTTP privacy boundaries."""

import asyncio
import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import errors, types

from pozor import admin_pipeline, semantic_profiles
from pozor.admin import AdminData, make_server
from pozor.admin_pipeline import AdminPipeline
from pozor.config import Settings
from pozor.context import fuse, retrieve
from pozor.llm import LlamaClient
from pozor.privacy import forget
from pozor.storage import connect, migrate
from pozor.telegram_account import AccountError, TelegramAccount


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    migrate(path)
    with connect(path) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,0,0)")
        db.execute("""INSERT INTO members(chat_id,user_id,display_name,consent)
            VALUES (-1,42,'Alice',1),(-1,43,'No consent',0),(-2,42,'Alice elsewhere',1)""")
        text = "Играю в шахматы каждый вечер"
        db.execute("""INSERT INTO messages(scope_chat_id,source_namespace,source_message_id,
            author_id,text,text_hash,occurred_at,origin) VALUES (-1,'live:-1',1,42,?,?,?,'live')""",
                   (text, hashlib.sha256(text.encode()).hexdigest(), datetime.now(UTC).isoformat()))
        db.execute("INSERT INTO message_subjects VALUES (1,42,'self','observed_self')")
    return path


def model_response(request):
    body = json.loads(request.content)
    if request.url.path == "/apply-template":
        return httpx.Response(200, json={"prompt": "rendered"})
    if request.url.path == "/tokenize":
        return httpx.Response(200, json={"tokens": list(range(100))})
    rows = json.loads(body["messages"][1]["content"])
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"observations": [{"topic": "Шахматы", "summary": "Рассказывает об игре в шахматы",
                                                "kind": "interest", "source_ids": [r["id"] for r in rows[:3]]}]})}}]})


@pytest.fixture
def pipeline(database, monkeypatch):
    settings = Settings(_env_file=None)
    monkeypatch.setattr(semantic_profiles, "LlamaClient", lambda settings: LlamaClient(
        settings, transport=httpx.MockTransport(model_response)))
    service = AdminPipeline(database, settings)
    service.account = SimpleNamespace(
        state="connected", entities={-1: SimpleNamespace(id=1)},
        open=AsyncMock(), close=AsyncMock(), disconnect=AsyncMock(),
        status=AsyncMock(return_value={"state": "connected", "configured": True, "qr": None}),
        groups=AsyncMock(return_value=[{"id": -1, "title": "Test group"}]),
        history=AsyncMock(return_value={42: [{"id": 100, "type": "message",
                    "date_unixtime": str(int(time.time())), "from_id": "user42", "text": "Люблю шахматы"}]}))
    yield service
    service.close()


def start_job(pipeline):
    now = datetime.now(UTC)
    return pipeline.call("start", {"chat": -1, "targets": [42],
                         "from": (now - timedelta(days=1)).date().isoformat(),
                         "until": (now + timedelta(days=1)).date().isoformat()})


def wait_job(pipeline):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = pipeline.call("job")
        if result["state"] != "running":
            return result
        time.sleep(.01)
    pytest.fail("job did not finish")


def test_history_import_review_save_and_admin_read(database, pipeline):
    start_job(pipeline)
    job = wait_job(pipeline)
    assert job["state"] == "review", job
    assert job["imported"] == 1
    draft = job["drafts"][0]
    assert draft["observations"][0]["sources"]
    assert pipeline.call("save", {"draft": draft["id"], "selections": [
        {"index": 0, "summary": "Обсуждает шахматы"}]}) == {"saved": 1}
    profile = AdminData(database).read("/api/profiles", {"chat": ["-1"]})["profiles"][0]
    assert profile["cards"][0]["summary"] == "Обсуждает шахматы"
    assert profile["cards"][0]["profile_version"] == 2
    from unittest.mock import patch

    with patch("pozor.context.fuse", wraps=fuse) as ranking:
        retrieve(database, chat_id=-1, thread_id=0, target_id=42, trigger_id=999,
                 hint="Игра", target_label="Alice", cards_enabled=False)
    assert ranking.call_args.args[0][3][1]  # Reviewed evidence works without automatic cards.
    assert not list((database.parent / "staging").glob("account-*.json"))
    with pytest.raises(AccountError):
        pipeline.call("save", {"draft": draft["id"], "selections": []})


@pytest.mark.parametrize("mutation", ["forget", "opt_out", "edit", "expiry", "disabled"])
def test_draft_cannot_resurrect_changed_or_forbidden_sources(database, pipeline, mutation):
    start_job(pipeline)
    draft = wait_job(pipeline)["drafts"][0]
    source = draft["observations"][0]["sources"][0]
    if mutation == "forget":
        forget(database, -1, 42)
    else:
        with connect(database) as db:
            if mutation == "opt_out":
                db.execute("UPDATE members SET opt_out=1 WHERE chat_id=-1 AND user_id=42")
            elif mutation == "edit":
                db.execute("UPDATE messages SET text_hash='changed' WHERE id=?", (source["id"],))
            elif mutation == "disabled":
                db.execute("UPDATE chats SET enabled=0 WHERE chat_id=-1")
            else:
                db.execute("UPDATE imports SET retained_until='2000-01-01' WHERE chat_id=-1")
    with pytest.raises(AccountError):
        pipeline.call("save", {"draft": draft["id"], "selections": [{"index": 0, "summary": "x"}]})
    assert pipeline.call("job")["drafts"] == []


def test_scope_bad_input_and_duplicate_jobs(pipeline):
    assert pipeline.call("members", {"chat": -1})["members"][0]["user_id"] == 42
    for body in ({"chat": -1, "targets": [43]}, {"chat": -2, "targets": [42]},
                 {"chat": -1, "targets": [42, 42]}, {"chat": True, "targets": [42]}):
        with pytest.raises(AccountError):
            pipeline.call("start", body | {"from": "2026-01-01", "until": "2026-02-01"})
    async def slow(*args):
        await asyncio.sleep(60)
    pipeline.account.history = slow
    start_job(pipeline)
    with pytest.raises(AccountError):
        start_job(pipeline)
    pipeline.call("cancel")
    assert pipeline.call("job")["state"] == "cancelled"
    assert pipeline.call("job")["drafts"] == []


def test_forget_while_downloading_prevents_import(database, pipeline):
    async def history(*args):
        forget(database, -1, 42)
        return {42: [{"id": 100, "date_unixtime": str(int(time.time())),
                      "from_id": "user42", "text": "Should not return"}]}
    pipeline.account.history = history
    start_job(pipeline)
    assert wait_job(pipeline)["state"] == "failed"
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_consent_revoked_at_activation_is_checked_inside_transaction(database, pipeline, monkeypatch):
    original = admin_pipeline.apply_import
    def revoke_then_apply(*args, **kwargs):
        with connect(database) as db:
            db.execute("UPDATE members SET consent=0 WHERE chat_id=-1 AND user_id=42")
        return original(*args, **kwargs)
    monkeypatch.setattr(admin_pipeline, "apply_import", revoke_then_apply)
    start_job(pipeline)
    assert wait_job(pipeline)["state"] == "failed"
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM imports WHERE status='active'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM messages WHERE origin='export'").fetchone()[0] == 0


def test_model_cannot_invent_evidence(database, monkeypatch):
    def invented(request):
        response = model_response(request)
        if request.url.path.endswith("completions"):
            value = response.json()
            content = json.loads(value["choices"][0]["message"]["content"])
            content["observations"][0]["source_ids"] = [9999]
            value["choices"][0]["message"]["content"] = json.dumps(content)
            return httpx.Response(200, json=value)
        return response
    monkeypatch.setattr(semantic_profiles, "LlamaClient", lambda settings: LlamaClient(
        settings, transport=httpx.MockTransport(invented)))
    with pytest.raises(Exception, match="invalid_profile_output"):
        asyncio.run(semantic_profiles.analyze(database, Settings(_env_file=None), -1, 42, lambda *a: None))


def test_http_mutations_require_auth_origin_and_json(database, pipeline):
    server = make_server(AdminData(database), "test-admin-password-123", port=0, pipeline=pipeline)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
            assert client.get("/api/telegram/status").status_code == 401
            assert client.post("/api/telegram/cancel", json={}).status_code == 401
            assert client.get("/pipeline.js").status_code == 200
            client.post("/api/login", json={"password": "test-admin-password-123"})
            assert client.get("/api/telegram/status").json()["state"] == "connected"
            assert client.get("/api/telegram/members?chat=-1").json()["members"][0]["user_id"] == 42
            assert client.post("/api/telegram/cancel", json={}, headers={"Origin": "https://evil.test"}).status_code == 403
            assert client.post("/api/telegram/cancel", content="{}", headers={"Content-Type": "text/plain"}).status_code == 400
            assert client.post("/api/telegram/start", json={}).status_code == 400
            assert client.post("/api/telegram/cancel", json={}).status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_account_qr_2fa_and_disconnect(tmp_path):
    account = TelegramAccount(Settings(_env_file=None, telegram_api_id=123,
                                       telegram_api_hash="a" * 32), tmp_path)
    login = SimpleNamespace(url="tg://login?token=test", wait=AsyncMock(
        side_effect=errors.SessionPasswordNeededError(request=None)))
    client = SimpleNamespace(qr_login=AsyncMock(return_value=login), sign_in=AsyncMock(),
                             disconnect=AsyncMock(), log_out=AsyncMock(return_value=True))
    account.client = client
    assert (await account.start())["qr"].startswith("data:image/svg+xml;base64,")
    await asyncio.sleep(0)
    assert (await account.status())["state"] == "password"
    assert (await account.password("secret"))["state"] == "connected"
    session = tmp_path / "account.session"
    session.write_text("secret", "utf-8")
    await account.disconnect()
    client.log_out.assert_awaited_once()
    assert not session.exists()
    assert account.state == "disconnected"


@pytest.mark.asyncio
async def test_account_filters_group_dates_and_authors(tmp_path):
    account = TelegramAccount(Settings(_env_file=None), tmp_path)
    account.entities[-1] = types.PeerChat(chat_id=1)
    now = datetime.now(UTC)
    async def messages(*args, **kwargs):
        for sender, date in [(42, now), (43, now), (42, now - timedelta(days=10))]:
            yield SimpleNamespace(date=date, sender_id=sender, message="text", id=1)
    account.client = SimpleNamespace(iter_messages=messages)
    result = await account.history(-1, {42}, now - timedelta(days=1),
                                   now + timedelta(days=1), lambda n: None)
    assert len(result[42]) == 1
    assert result[42][0]["from_id"] == "user42"


def test_semantic_sample_respects_requested_period(database):
    now = datetime.now(UTC)
    assert semantic_profiles.snapshot(database, -1, 42)[1]
    assert not semantic_profiles.snapshot(database, -1, 42, now - timedelta(days=10),
                                           now - timedelta(days=1))[1]
