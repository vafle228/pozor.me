import sqlite3
from pathlib import Path

from pozor.infra.sqlite.connection import connect


def fts5_available() -> tuple[bool,str]:
    try:
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE VIRTUAL TABLE doctor_fts USING fts5(content)')
        return True,sqlite3.sqlite_version
    except sqlite3.Error:
        return False,'SQLite was built without FTS5'


def schema(database: Path) -> str:
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
        return str(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0])


def register_bot(database: Path, user: int) -> None:
    with connect(database) as db:
        db.execute('INSERT OR IGNORE INTO bot_identities VALUES (?)',(user,))


def allowed_chats(database: Path) -> list[int]:
    with connect(database) as db:
        return [r[0] for r in db.execute('SELECT chat_id FROM chats WHERE allowed=1 AND enabled=1 ORDER BY chat_id DESC')]


def exclude_hook(database: Path, item: int) -> None:
    with connect(database) as db:
        db.execute("UPDATE profile_items SET excluded=1 WHERE id=? AND kind='hook'",(item,))


def chat_level(database: Path, chat: int, level: int) -> None:
    if level not in (1,2,3):
        raise ValueError('level must be between 1 and 3')
    with connect(database) as db:
        db.execute('UPDATE chats SET roast_level=? WHERE chat_id=?',(level,chat))
        db.execute("UPDATE sessions SET state='closed',closed_reason='level_changed',revision=revision+1 "
                   "WHERE chat_id=? AND state='active' AND roast_level>?",(chat,level))
