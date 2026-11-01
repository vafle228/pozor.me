from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from pozor.context import sources_current
from pozor.core.humor.candidates import SYSTEM


class AdminData:
    def __init__(self, database: Path, profiles_enabled: bool = False,
                 semantic_profiles_enabled: bool = True):
        self.database = database.resolve()
        self.profiles_enabled = profiles_enabled
        self.semantic_profiles_enabled = semantic_profiles_enabled

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
        if path == "/api/members":
            chat = int(query["chat"][0])
            if not db.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat,)).fetchone():
                raise LookupError
            rows = db.execute("""SELECT m.user_id,m.display_name,m.current_username,
                m.consent,m.opt_out,EXISTS(SELECT 1 FROM forgotten_members f
                WHERE f.chat_id=m.chat_id AND f.user_id=m.user_id) AS forgotten
                FROM members m WHERE m.chat_id=?
                ORDER BY coalesce(m.display_name,''),m.user_id""", (chat,)).fetchall()
            return {"members": [dict(row) for row in rows]}
        if path == "/api/chats":
            rows = db.execute("""SELECT c.*,
                (SELECT count(*) FROM members m WHERE m.chat_id=c.chat_id) AS members,
                (SELECT max(occurred_at) FROM messages m
                 WHERE m.scope_chat_id=c.chat_id) AS last_message
                FROM chats c ORDER BY c.created_at DESC,c.chat_id""").fetchall()
            return {"chats": [dict(row) for row in rows],
                    "profiles_enabled": self.profiles_enabled,
                    "semantic_profiles_enabled": self.semantic_profiles_enabled}
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
                for row in db.execute("""SELECT * FROM profile_items WHERE chat_id=?
                        AND target_id=? ORDER BY last_at DESC,id DESC""",
                                      (chat, member["user_id"])).fetchall():
                    sources = db.execute("""SELECT m.id,m.text,m.author_id,m.occurred_at,
                        m.source_message_id,m.text_hash,ce.source_text_hash
                        FROM profile_item_sources ce JOIN messages m ON m.id=ce.message_id
                        WHERE ce.card_id=? ORDER BY m.occurred_at,m.id""", (row["id"],)).fetchall()
                    ids = [f"message:{r['id']}" for r in sources]
                    if (not sources or any(r["text_hash"] != r["source_text_hash"] for r in sources)
                            or not sources_current(db, chat, member["user_id"], ids, time.time())):
                        continue
                    cards.append(dict(row) | {"sources": [
                        {k: r[k] for k in ("id", "text", "author_id", "occurred_at",
                                          "source_message_id")} for r in sources]})
                automatic = db.execute('SELECT state,analyzed,sampled,updated_at,error_code '
                                       'FROM automatic_profiles WHERE chat_id=? AND target_id=?',
                                       (chat, member['user_id'])).fetchone()
                result.append(dict(member) | {"cards": cards,
                              "automatic": dict(automatic) if automatic else None})
            return {"profiles": result, "total": len(members), "offset": offset}
        if path == "/api/evidence":
            chat, target = int(query["chat"][0]), int(query["target"][0])
            if not db.execute("""SELECT 1 FROM members m WHERE chat_id=? AND user_id=?
                AND consent=1 AND opt_out=0 AND NOT EXISTS (SELECT 1 FROM forgotten_members f
                WHERE f.chat_id=m.chat_id AND f.user_id=m.user_id)""", (chat, target)).fetchone():
                raise LookupError
            quotes: list[dict[str, object]] = []
            related: dict[int, dict[str, object]] = {}
            for row in db.execute("""SELECT m.id,m.text,m.author_id,m.occurred_at,
                m.source_message_id,m.origin,s.relation,s.attribution
                FROM messages m JOIN message_subjects s
                ON s.message_id=m.id WHERE m.scope_chat_id=? AND s.target_id=?
                AND ((m.author_id=? AND s.relation='self'
                      AND s.attribution IN ('observed_self','imported_quote'))
                     OR (m.author_id<>? AND s.relation IN ('mention','reply')
                         AND s.attribution='third_party'))
                ORDER BY m.occurred_at DESC,m.id DESC,s.relation DESC""",
                                  (chat, target, target, target)):
                if sources_current(db, chat, target, [f"message:{row['id']}"], time.time()):
                    if row["relation"] == "self":
                        if len(quotes) < 100:
                            quotes.append(dict(row))
                    elif len(related) < 100:
                        related.setdefault(row["id"], dict(row))
            return {"quotes": quotes, "related": list(related.values())}
        if path == "/api/prompt":
            return {"system": SYSTEM, "sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
                    "source": "src/pozor/prompts/roast_system.txt"}
        raise LookupError


