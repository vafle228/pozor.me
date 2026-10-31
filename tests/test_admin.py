"""Read-only HTTP boundary and chat-scoped profile privacy regressions."""

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pozor.admin import AdminData, make_server
from pozor.llm import SYSTEM
from pozor.privacy import forget
from pozor.profiles import commit_batch, source_rows
from pozor.storage import connect, migrate


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
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=Strict" in response.headers["set-cookie"]


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
    assert client.get("/api/prompt").json()["system"] == SYSTEM
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
