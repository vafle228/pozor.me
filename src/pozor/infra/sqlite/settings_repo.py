import time
from pathlib import Path
from typing import Any

from pozor.infra.sqlite.connection import connect


class SettingsRepository:
    def __init__(self, database: Path):
        self.database = database

    def overrides(self) -> dict[str, str]:
        with connect(self.database) as db:
            return {r['name']:r['value_json'] for r in db.execute('SELECT name,value_json FROM settings_overrides')}

    def save(self, name: str, value_json: str | None, actor: str) -> None:
        with connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT value_json FROM settings_overrides WHERE name=?',(name,)).fetchone()
            db.execute('INSERT INTO settings_audit(name,previous_json,value_json,updated_by,updated_at) VALUES (?,?,?,?,?)',
                       (name,old[0] if old else None,value_json,actor,time.time()))
            if value_json is None:
                db.execute('DELETE FROM settings_overrides WHERE name=?',(name,))
            else:
                db.execute('INSERT INTO settings_overrides VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                    'value_json=excluded.value_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at',
                    (name,value_json,actor,time.time()))

    def retention(self, live_days: int, import_days: int) -> None:
        with connect(self.database) as db:
            db.execute('UPDATE chats SET retention_days=?',(live_days,))
            db.execute("UPDATE imports SET retained_until=datetime(created_at,'+' || ? || ' days') WHERE status='active'",(import_days,))

    def history(self) -> list[dict[str,Any]]:
        with connect(self.database) as db:
            return [dict(r) for r in db.execute('SELECT * FROM settings_audit ORDER BY id DESC LIMIT 100')]
