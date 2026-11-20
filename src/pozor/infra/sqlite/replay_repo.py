"""Read legacy delivery outcomes from the synthetic replay database."""

from pathlib import Path
from typing import Any

from pozor.infra.sqlite.connection import connect


def delivery_outcomes(database: Path) -> list[dict[str, Any]]:
    with connect(database) as db:
        rows = db.execute(
            """SELECT s.trigger_message_id AS session_trigger, s.thread_id, s.target_id,
                      t.turn_no, t.status, t.fallback, t.generation_reason,
                      o.status AS outbox, o.error_code, s.closed_reason
               FROM sessions s JOIN turns t ON t.session_id=s.id
               LEFT JOIN outbox o ON o.session_id=s.id AND o.turn_no=t.turn_no
               ORDER BY s.trigger_message_id,t.turn_no"""
        )
        return [dict(row) for row in rows]
