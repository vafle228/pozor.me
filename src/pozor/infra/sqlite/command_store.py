"""Short command transactions used through asyncio.to_thread by Telegram handlers."""

from datetime import UTC, datetime
from pathlib import Path

from pozor.infra.sqlite.commands import _command_allowed
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.privacy_repo import blocked


def allowed(database: Path, chat: int, *, enabled: bool) -> bool:
    with connect(database) as db:
        return _command_allowed(db,chat,require_enabled=enabled)


def is_blocked(database: Path, chat: int, user: int) -> bool:
    with connect(database) as db:
        return blocked(db,chat,user)


def observe(database: Path, chat: int, user: int, display_name: str, username: str | None) -> None:
    stamp = datetime.now(UTC).isoformat(timespec='seconds')
    with connect(database) as db:
        db.execute('''INSERT INTO members(chat_id,user_id,display_name,current_username,last_seen)
            SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM chats WHERE chat_id=? AND allowed=1)
            AND NOT EXISTS (SELECT 1 FROM forgotten_members WHERE chat_id=? AND user_id=?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET display_name=excluded.display_name,
            current_username=excluded.current_username,last_seen=excluded.last_seen''',
            (chat,user,display_name,username,stamp,chat,chat,user))
        if not username or blocked(db,chat,user):
            return
        current = db.execute('SELECT id,username FROM aliases WHERE chat_id=? AND user_id=? AND valid_to IS NULL',
                             (chat,user)).fetchall()
        for alias in current:
            if alias['username'].casefold()!=username.casefold():
                db.execute('UPDATE aliases SET valid_to=? WHERE id=?',(stamp,alias['id']))
        if not any(a['username'].casefold()==username.casefold() for a in current):
            db.execute('''INSERT INTO aliases(chat_id,user_id,username,valid_from,source)
                SELECT ?,?,?,?,'live' WHERE EXISTS (SELECT 1 FROM members WHERE chat_id=? AND user_id=?)''',
                (chat,user,username,stamp,chat,user))


def consent_level(database: Path, chat: int, user: int, level: int) -> None:
    with connect(database) as db:
        db.execute('DELETE FROM forgotten_members WHERE chat_id=? AND user_id=?',(chat,user))
        db.execute('UPDATE members SET roast_level=? WHERE chat_id=? AND user_id=?',(level,chat,user))
        db.execute("UPDATE sessions SET state='closed',closed_reason='level_changed',revision=revision+1 "
                   "WHERE chat_id=? AND target_id=? AND state='active' AND roast_level>?",(chat,user,level))


def label(database: Path, chat: int, user: int) -> str:
    with connect(database) as db:
        row = db.execute('SELECT display_name FROM members WHERE chat_id=? AND user_id=?',(chat,user)).fetchone()
        return row['display_name'] if row and row['display_name'] else 'участник'


def receipt(database: Path, chat: int, message: int, text: str) -> bool:
    with connect(database) as db:
        return db.execute('INSERT OR IGNORE INTO command_receipts(chat_id,message_id,result_text) VALUES (?,?,?)',
                          (chat,message,text)).rowcount>0
