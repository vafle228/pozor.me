"""Async message-store port backed by the canonical entitlement view."""

import asyncio
from pathlib import Path

from pozor.core.domain.context import ContextPack
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.retrieval import retrieve, sources_current


class SQLiteMessageStore:
    def __init__(self, database: Path):
        self.database = database

    async def context(self, *, chat_id: int, thread_id: int, target_id: int,
                      trigger_id: int, hint: str, target_label: str) -> ContextPack:
        return await asyncio.to_thread(retrieve, self.database, chat_id=chat_id,
            thread_id=thread_id, target_id=target_id, trigger_id=trigger_id,
            hint=hint, target_label=target_label)

    async def entitled(self, chat: int, target: int, ids: list[str], now: float) -> bool:
        def check() -> bool:
            with connect(self.database) as db:
                return sources_current(db, chat, target, ids, now)
        return await asyncio.to_thread(check)
