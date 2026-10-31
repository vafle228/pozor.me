"""Local, authenticated, read-only administration: python -m pozor.admin."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pozor.config import Settings
from pozor.context import sources_current
from pozor.llm import SYSTEM

STATIC = Path(__file__).with_name("admin_static")


class AdminData:
    def __init__(self, database: Path, profiles_enabled: bool = False):
        self.database = database.resolve()
        self.profiles_enabled = profiles_enabled

    def read(self, path: str, query: dict[str, list[str]]) -> object:
        # A typo must never create an empty production database. No migrations or writes.
        db = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")  # Privacy checks and returned quotes share one snapshot.
            return self._read(db, path, query)
        finally:
            db.close()

    def _read(self, db: sqlite3.Connection, path: str, query: dict[str, list[str]]) -> object:
        if path == "/api/chats":
            rows = db.execute("""SELECT c.*,
                (SELECT count(*) FROM members m WHERE m.chat_id=c.chat_id) AS members,
                (SELECT max(occurred_at) FROM messages m
                 WHERE m.scope_chat_id=c.chat_id) AS last_message
                FROM chats c ORDER BY c.created_at DESC,c.chat_id""").fetchall()
            return {"chats": [dict(row) for row in rows],
                    "profiles_enabled": self.profiles_enabled}
        if path == "/api/profiles":
            chat = int(query["chat"][0])
            search = query.get("q", [""])[0].strip().casefold()
            offset = max(0, int(query.get("offset", ["0"])[0]))
            if not db.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat,)).fetchone():
                raise LookupError
            members = db.execute("""SELECT m.* FROM members m WHERE m.chat_id=?
                AND m.opt_out=0 AND m.consent=1 AND NOT EXISTS
                (SELECT 1 FROM forgotten_members f WHERE f.chat_id=m.chat_id
                 AND f.user_id=m.user_id)
                ORDER BY coalesce(m.display_name,''),m.user_id""", (chat,)).fetchall()
            members = [m for m in members if search in
                       f"{m['display_name'] or ''} {m['current_username'] or ''} {m['user_id']}".casefold()]
            result = []
            for member in members[offset:offset + 30]:
                cards = []
                for row in db.execute("""SELECT * FROM profile_cards WHERE chat_id=?
                        AND target_id=? ORDER BY last_at DESC,id DESC""",
                                      (chat, member["user_id"])).fetchall():
                    sources = db.execute("""SELECT m.id,m.text,m.author_id,m.occurred_at,
                        m.source_message_id,m.text_hash,ce.source_text_hash
                        FROM card_evidence ce JOIN messages m ON m.id=ce.message_id
                        WHERE ce.card_id=? ORDER BY m.occurred_at,m.id""", (row["id"],)).fetchall()
                    ids = [f"message:{r['id']}" for r in sources]
                    if (not sources or any(r["text_hash"] != r["source_text_hash"] for r in sources)
                            or not sources_current(db, chat, member["user_id"], ids, time.time())):
                        continue
                    cards.append(dict(row) | {"sources": [
                        {k: r[k] for k in ("id", "text", "author_id", "occurred_at",
                                          "source_message_id")} for r in sources]})
                result.append(dict(member) | {"cards": cards})
            return {"profiles": result, "total": len(members), "offset": offset}
        if path == "/api/prompt":
            return {"system": SYSTEM, "sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
                    "source": "src/pozor/prompts/roast_system.txt"}
        raise LookupError


def make_server(data: AdminData, password: str, host: str = "127.0.0.1",
                port: int = 8090) -> ThreadingHTTPServer:
    if len(password) < 16:
        raise ValueError("ADMIN_PASSWORD должен содержать минимум 16 символов")
    key = secrets.token_bytes(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # No passwords, request bodies or profile text in access logs.

        def reply(self, status: int, body: bytes, content_type: str = "application/json",
                  cookie: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; "
                             "script-src 'self'; style-src 'self'; connect-src 'self'; "
                             "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self) -> bool:
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                stamp, signature = cookies["pozor_admin"].value.split(".")
                expected = hmac.new(key, stamp.encode(), "sha256").hexdigest()
                return 0 <= time.time() - int(stamp) < 28800 and hmac.compare_digest(
                    signature, expected)
            except (KeyError, ValueError):
                return False

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path in ("/", "/app.js", "/style.css"):
                filename, mime = {"/": ("index.html", "text/html"),
                                  "/app.js": ("app.js", "text/javascript"),
                                  "/style.css": ("style.css", "text/css")}[parsed.path]
                self.reply(200, (STATIC / filename).read_bytes(), mime)
                return
            if not self.authenticated():
                self.reply(401, b'{"error":"authentication_required"}')
                return
            try:
                result = data.read(parsed.path, parse_qs(parsed.query))
                self.reply(200, json.dumps(result, ensure_ascii=False).encode())
            except (ValueError, KeyError, IndexError):
                self.reply(400, b'{"error":"invalid_query"}')
            except LookupError:
                self.reply(404, b'{"error":"not_found"}')
            except sqlite3.Error:
                self.reply(503, b'{"error":"database_unavailable"}')

        def do_POST(self) -> None:
            # Browser cross-origin forms/fetch cannot submit credentials or log out.
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.reply(403, b'{}')
                return
            if self.path == "/api/logout":
                self.reply(200, b'{}', cookie="pozor_admin=; Path=/; HttpOnly; "
                           "SameSite=Strict; Max-Age=0")
                return
            if self.path != "/api/login":
                self.reply(404, b'{}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError
                body = json.loads(self.rfile.read(size))
                supplied = body.get("password") if isinstance(body, dict) else None
                if not isinstance(supplied, str):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self.reply(400, b'{}')
                return
            if not hmac.compare_digest(supplied.encode(), password.encode()):
                self.reply(401, b'{"error":"invalid_password"}')
                return
            stamp = str(int(time.time()))
            signature = hmac.new(key, stamp.encode(), "sha256").hexdigest()
            self.reply(200, b'{}', cookie=f"pozor_admin={stamp}.{signature}; Path=/; "
                       "HttpOnly; SameSite=Strict; Max-Age=28800")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная админка pozor.me")
    parser.add_argument("--database", type=Path, help="SQLite бота; по умолчанию DATABASE_PATH")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    settings = Settings()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        from dotenv import dotenv_values

        password = dotenv_values(".env").get("ADMIN_PASSWORD") or ""
    database = args.database or settings.database_path
    if not database.is_file():
        parser.error("База не найдена. Укажите существующую базу бота через --database.")
    try:
        server = make_server(AdminData(database, settings.profile_cards_enabled),
                             password, port=args.port)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Админка: http://127.0.0.1:{server.server_port} (Ctrl+C — остановить)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
