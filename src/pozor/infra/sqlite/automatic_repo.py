import time
from pathlib import Path
from typing import Any

from pozor.infra.sqlite.connection import connect


def begin(database: Path, chat: int, target: int, rows: list[dict[str, Any]], signature: str) -> bool:
    now = time.time()
    with connect(database) as db:
        old = db.execute('SELECT * FROM automatic_profiles WHERE chat_id=? AND target_id=?',(chat,target)).fetchone()
        dossier = db.execute("SELECT 1 FROM profile_items WHERE chat_id=? AND target_id=? AND kind='dossier'",(chat,target)).fetchone()
        if old and old['signature']==signature and (old['state'] in {'empty','insufficient'} or old['state']=='ready' and dossier):
            return False
        if old and now-old['attempted_at']<60:
            return False
        if (old and old['signature'] and old['state'] in {'ready','empty'}
                and sum(r['id']>old['message_id'] for r in rows)<20 and now-old['updated_at']<600):
            return False
        db.execute('INSERT INTO automatic_profiles(chat_id,target_id) VALUES (?,?) ON CONFLICT DO NOTHING',(chat,target))
        db.execute('UPDATE automatic_profiles SET state=?,attempted_at=?,analyzed=0,sampled=?,error_code=NULL WHERE chat_id=? AND target_id=?',
                   ('running' if len(rows)>=10 else 'insufficient',now,len(rows),chat,target))
        if len(rows)<10:
            db.execute('UPDATE automatic_profiles SET signature=? WHERE chat_id=? AND target_id=?',(signature,chat,target))
            return False
    return True


def progress(database: Path, chat: int, target: int, done: int, total: int) -> None:
    with connect(database) as db:
        db.execute('UPDATE automatic_profiles SET analyzed=?,sampled=? WHERE chat_id=? AND target_id=?',(done,total,chat,target))


def finish(database: Path, chat: int, target: int, draft: dict[str, Any], ready: bool) -> None:
    with connect(database) as db:
        db.execute('UPDATE automatic_profiles SET signature=?,message_id=?,updated_at=?,state=?,analyzed=sampled '
                   'WHERE chat_id=? AND target_id=?',(draft['signature'],draft['message_id'],time.time(),
                                                     'ready' if ready else 'empty',chat,target))


def state(database: Path, chat: int, target: int, value: str, error: str | None = None) -> None:
    with connect(database) as db:
        db.execute('UPDATE automatic_profiles SET state=?,error_code=? WHERE chat_id=? AND target_id=?',(value,error,chat,target))


def targets(database: Path) -> list[dict[str, Any]]:
    with connect(database) as db:
        return [dict(r) for r in db.execute('''SELECT m.chat_id,m.user_id FROM members m JOIN chats c USING(chat_id)
            WHERE m.consent=1 AND m.opt_out=0 AND c.allowed=1 AND c.enabled=1
            AND NOT EXISTS (SELECT 1 FROM forgotten_members f WHERE f.chat_id=m.chat_id AND f.user_id=m.user_id)
            ORDER BY (SELECT count(*) FROM messages msg WHERE msg.scope_chat_id=m.chat_id AND msg.author_id=m.user_id) DESC''')]
