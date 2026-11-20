"""Authenticated HTTP boundary and chat-scoped profile privacy regressions."""

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pozor.admin import AdminData
from pozor.core.humor.candidates import SYSTEM
from pozor.privacy import forget
from pozor.profiles import commit_batch, source_rows
from pozor.storage import connect, migrate
from tests.admin_harness import make_server


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "admin.sqlite3"
    migrate(path)
    with connect(path) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,0,0)")
        db.execute("""INSERT INTO members(chat_id,user_id,display_name,consent)
            VALUES (-1,42,'Alice <script>',1),(-2,42,'Another context',1),
                   (-1,43,'No consent',0)""")
        for mid, chat, text in [(1, -1, "First chat quote"), (2, -2, "Second chat quote")]:
            db.execute("""INSERT INTO messages(id,scope_chat_id,source_namespace,
                source_message_id,author_id,text,text_hash,occurred_at,origin)
                VALUES (?,?,?,?,42,?,?,?,'live')""",
                       (mid, chat, f"live:{chat}", mid, text,
                        hashlib.sha256(text.encode()).hexdigest(), datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO message_subjects VALUES (?,42,'self','observed_self')", (mid,))
    for chat in (-1, -2):
        with connect(path) as db:
            rows = [dict(r) for r in source_rows(db, chat, 42)]
        assert commit_batch(path, chat, 42, 0, rows)
    return path


@pytest.fixture
def client(database):
    server = make_server(AdminData(database), "test-admin-password-123", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        yield client
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def login(client):
    response = client.post("/api/login", json={"password": "test-admin-password-123"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Strict" in response.headers["set-cookie"]


def test_evidence_requires_auth_consent_scope_and_current_sources(client, database):
    url = "/api/evidence?chat=-1&target=42"
    assert client.get(url).status_code == 401
    login(client)
    assert [q["text"] for q in client.get(url).json()["quotes"]] == ["First chat quote"]
    assert client.get("/api/evidence?chat=-1&target=43").status_code == 404
    with connect(database) as db:
        db.execute("UPDATE messages SET occurred_at='2000-01-01' WHERE scope_chat_id=-1")
    assert client.get(url).json()["quotes"] == []
    forget(database, -1, 42)
    assert client.get(url).status_code == 404


def test_http_auth_static_and_logout(client):
    for path in ("/api/chats", "/api/profiles?chat=-1", "/api/prompt"):
        assert client.get(path).status_code == 401
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    login(client)
    assert len(client.get("/api/chats").json()["chats"]) == 2
    assert client.get("/api/prompt").json()["system"].strip() == SYSTEM.strip()
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/prompt").status_code == 401


def test_scope_search_pagination_and_input(client):
    login(client)
    first = client.get("/api/profiles?chat=-1").json()
    assert first["total"] == 1  # No-consent member never appears.
    assert first["profiles"][0]["cards"][0]["summary"] == "First chat quote"
    assert "Second chat quote" not in str(first)
    assert client.get("/api/profiles?chat=-2").json()["profiles"][0]["display_name"] == "Another context"
    assert client.get("/api/profiles?chat=-1&q=ALICE").json()["total"] == 1
    assert client.get("/api/profiles?chat=-1&q=missing").json()["total"] == 0
    assert client.get("/api/profiles?chat=-1&offset=30").json()["profiles"] == []
    for query in ("", "?chat=abc", "?chat=-1&offset=abc", "?chat=1%20OR%201=1"):
        assert client.get("/api/profiles" + query).status_code == 400
    assert client.get("/api/profiles?chat=-999").status_code == 404


@pytest.mark.parametrize("mutation", ["expired", "forgotten", "opt_out", "edited"])
def test_privacy_invalidation(database, mutation):
    data = AdminData(database)
    assert data.read("/api/profiles", {"chat": ["-1"]})["profiles"][0]["cards"]
    if mutation == "forgotten":
        forget(database, -1, 42)
    else:
        with connect(database) as db:
            if mutation == "expired":
                db.execute("UPDATE messages SET occurred_at=? WHERE id=1",
                           ((datetime.now(UTC) - timedelta(days=31)).isoformat(),))
            elif mutation == "opt_out":
                db.execute("UPDATE members SET opt_out=1 WHERE chat_id=-1 AND user_id=42")
            else:
                db.execute("UPDATE messages SET text_hash='changed' WHERE id=1")
    result = data.read("/api/profiles", {"chat": ["-1"]})
    assert not result["profiles"] or not result["profiles"][0]["cards"]
    assert "First chat quote" not in str(result)
    assert data.read("/api/profiles", {"chat": ["-2"]})["profiles"][0]["cards"]


def test_cross_origin_and_malformed_login(client):
    assert client.post("/api/login", json={"password": "test-admin-password-123"},
                       headers={"Origin": "https://example.com"}).status_code == 403
    for body in ([], {"password": 123}, None):
        assert client.post("/api/login", json=body).status_code == 400
    assert client.post("/api/login", content="{").status_code == 400
    login(client)
    assert client.post("/api/logout", headers={"Origin": "https://example.com"}).status_code == 403
    assert client.get("/api/chats").status_code == 200


def test_read_only_database_and_missing_path(database, tmp_path):
    before = database.read_bytes()
    data = AdminData(database)
    data.read("/api/chats", {})
    data.read("/api/profiles", {"chat": ["-1"]})
    assert database.read_bytes() == before
    missing = tmp_path / "missing.sqlite3"
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        AdminData(missing).read("/api/chats", {})
    assert not missing.exists()


def test_member_roast_control_auth_scope_and_sessions(client, database):
    url = "/api/members/roast"
    payload = {"chat": -1, "user": 42, "enabled": False}
    assert client.get("/api/members?chat=-1").status_code == 401
    assert client.post(url, json=payload).status_code == 401
    login(client)
    members = client.get("/api/members?chat=-1").json()["members"]
    assert {m["user_id"] for m in members} == {42, 43}
    assert "First chat quote" not in str(members)
    assert "Another context" not in str(members)
    with connect(database) as db:
        db.execute("""INSERT INTO sessions
            (id,chat_id,target_id,initiator_id,state,created_at,trigger_message_id)
            VALUES ('admin-session',-1,42,43,'active',100,20)""")
    assert client.post(url, json=payload, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post(url, json=payload).status_code == 200
    assert client.get("/api/profiles?chat=-1").json()["total"] == 0
    with connect(database) as db:
        assert tuple(db.execute("SELECT consent,opt_out FROM members WHERE chat_id=-1 AND user_id=42").fetchone()) == (0, 1)
        assert tuple(db.execute("SELECT state,closed_reason FROM sessions WHERE id='admin-session'").fetchone()) == ("closed", "opt_out")
        assert db.execute("SELECT consent FROM members WHERE chat_id=-2 AND user_id=42").fetchone()[0] == 1
        epoch = db.execute("SELECT epoch FROM data_epochs WHERE chat_id=-1 AND target_id=42").fetchone()[0]
    assert client.post(url, json=payload).status_code == 200
    with connect(database) as db:
        assert db.execute("SELECT epoch FROM data_epochs WHERE chat_id=-1 AND target_id=42").fetchone()[0] == epoch
    assert client.post(url, json={**payload, "enabled": True}).status_code == 200
    assert client.post(url, json={**payload, "user": 43, "enabled": True}).status_code == 200
    assert client.get("/api/profiles?chat=-1").json()["total"] == 2
    forget(database, -1, 42)
    assert client.post(url, json={**payload, "enabled": True}).status_code == 409
    member = next(m for m in client.get("/api/members?chat=-1").json()["members"] if m["user_id"] == 42)
    assert member["forgotten"] == 1 and member["consent"] == 0


def test_member_roast_validation(client):
    login(client)
    payload = {"chat": -1, "user": 43, "enabled": True}
    for changes in ({"enabled": "false"}, {"enabled": 1}, {"chat": True},
                    {"user": None}, {"user": 2**100}, {"chat": "-1"}):
        assert client.post("/api/members/roast", json={**payload, **changes}).status_code == 400
    for changes in ({"chat": -999}, {"user": 999}, {"chat": -2}):
        assert client.post("/api/members/roast", json={**payload, **changes}).status_code == 409
    assert client.post("/api/members/roast", content="{}").status_code == 400
    assert client.get("/api/members?chat=-999").status_code == 404
    assert client.get("/api/members?chat=bad").status_code == 400


def test_evidence_separates_own_messages_from_replies_mentions_and_unrelated(database):
    from pozor.commands import set_consent
    from pozor.context import retrieve
    from pozor.semantic_profiles import snapshot

    with connect(database) as db:
        db.execute("UPDATE members SET consent=0 WHERE chat_id=-1 AND user_id=42")
        for mid, relation in [(3, "reply"), (4, "mention"), (5, None)]:
            text = f"Other person's message {mid}"
            db.execute("""INSERT INTO messages(id,scope_chat_id,source_namespace,
                source_message_id,author_id,text,text_hash,occurred_at,origin)
                VALUES (?,-1,'live:-1',?,43,?,?,?,'live')""",
                       (mid, mid, text, hashlib.sha256(text.encode()).hexdigest(),
                        datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO message_subjects VALUES (?,43,'self','observed_self')", (mid,))
            if relation:
                db.execute("INSERT INTO message_subjects VALUES (?,42,?,'third_party')", (mid, relation))
        # A reply that also mentions the member must appear once.
        db.execute("INSERT INTO message_subjects VALUES (3,42,'mention','third_party')")
    assert set_consent(database, -1, 42, True, respect_forgetting=True)
    data = AdminData(database)
    result = data.read("/api/evidence", {"chat": ["-1"], "target": ["42"]})
    assert [r["id"] for r in result["quotes"]] == [1]
    assert {r["id"] for r in result["related"]} == {3, 4}
    assert all(r["author_id"] == 43 and r["attribution"] == "third_party" for r in result["related"])
    _, portrait = snapshot(database, -1, 42)
    assert [r["id"] for r in portrait] == [1]
    pack = retrieve(database, chat_id=-1, thread_id=0, target_id=42,
                    trigger_id=999, hint="message", target_label="Alice")
    rows = [*pack.recent, *pack.evidence]
    assert {r.source_message_id for r in rows} == {1, 3, 4}
    assert all(r.attribution == "third_party" for r in rows if r.author_id == 43)


def test_evidence_context_does_not_consume_own_message_limit(database):
    with connect(database) as db:
        for mid in range(10, 115):
            db.execute("""INSERT INTO messages(id,scope_chat_id,source_namespace,
                source_message_id,author_id,text,text_hash,occurred_at,origin)
                VALUES (?,-1,'live:-1',?,43,'reply','hash',?,'live')""",
                       (mid, mid, datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO message_subjects VALUES (?,42,'reply','third_party')", (mid,))
    result = AdminData(database).read("/api/evidence", {"chat": ["-1"], "target": ["42"]})
    assert [r["id"] for r in result["quotes"]] == [1]
    assert len(result["related"]) == 100
