import asyncio
import time
from pathlib import Path

from pydantic import ValidationError

from pozor.config import Settings
from pozor.core.domain.context import Hook
from pozor.core.humor.hooks import extract
from pozor.core.humor.material import normalize
from pozor.infra.llm.client import LlamaClient
from pozor.infra.llm.queue import InferenceQueue
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.profile_repo import snapshot
from pozor.infra.sqlite.retrieval import sources_current


def select_hooks(database: Path, chat: int, target: int, trigger: str, level: int,
                 used_sources: set[int]) -> list[Hook]:
    words = set(normalize(trigger).split())
    result: list[Hook] = []
    with connect(database) as db:
        for row in db.execute('''SELECT p.*,s.message_id,s.source_text_hash,m.text_hash,m.author_id
            FROM profile_items p JOIN profile_item_sources s ON s.card_id=p.id
            JOIN messages m ON m.id=s.message_id WHERE p.chat_id=? AND p.target_id=?
            AND p.kind='hook' AND p.excluded=0 AND p.heat<=?''',(chat,target,level)):
            if (row['message_id'] in used_sources or row['author_id'] != target
                    or row['source_text_hash'] != row['text_hash']
                    or not sources_current(db,chat,target,[f"message:{row['message_id']}"],time.time())):
                continue
            result.append(Hook(quote=row['quote'],why_funny=row['why_funny'],type=row['hook_type'],
                               heat=row['heat'],source_id=row['message_id']))
    return sorted(result,key=lambda h:(-len(words&set(normalize(h.quote).split())),h.heat,h.source_id))[:2]


async def refresh_hooks(database: Path, settings: Settings, queue: InferenceQueue,
                        chat: int, target: int) -> int:
    version, rows = await asyncio.to_thread(snapshot,database,chat,target)
    hooks: list[Hook] = []
    by_id = {r['id']:r for r in rows[:30]}
    async with LlamaClient(settings) as client:
        for offset in range(0,len(by_id),15):
            batch = list(by_id.values())[offset:offset+15]
            async def build(deadline: float, batch: list[dict[str, object]] = batch) -> list[Hook]:
                return await extract(client,{int(str(r['id'])):str(r['text'])[:2000] for r in batch},deadline,
                                     settings.hook_temperature)
            try:
                hooks.extend(await queue.submit(key=f'hooks:{chat}:{target}:{offset}',chat_id=chat,
                    factory=build,deadline=asyncio.get_running_loop().time()+settings.background_timeout_seconds,
                    background=True))
            except ValidationError:
                continue
    def commit() -> int:
        with connect(database) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT epoch FROM data_epochs WHERE chat_id=? AND target_id=?',(chat,target)).fetchone()
            if (current[0] if current else 0) != version:
                return 0
            member = db.execute('SELECT consent,opt_out FROM members WHERE chat_id=? AND user_id=?',(chat,target)).fetchone()
            if not member or not member['consent'] or member['opt_out']:
                return 0
            if not sources_current(db,chat,target,[f'message:{sid}' for sid in by_id],time.time()):
                return 0
            excluded = {r[0] for r in db.execute("SELECT quote FROM profile_items WHERE chat_id=? AND target_id=? AND kind='hook' AND excluded=1",(chat,target))}
            db.execute("DELETE FROM profile_items WHERE chat_id=? AND target_id=? AND kind='hook' AND excluded=0",(chat,target))
            saved = 0
            for hook in hooks[:8]:
                if hook.quote in excluded:
                    continue
                row = by_id[hook.source_id]
                cursor = db.execute('''INSERT INTO profile_items(chat_id,target_id,kind,provenance,topic,
                    summary,attribution,source_kind,first_at,last_at,episodes,quote,why_funny,hook_type,heat)
                    VALUES (?,?,'hook','automatic',?,?,'observed_self','self_quote',?,?,1,?,?,?,?)''',
                    (chat,target,hook.type,hook.why_funny,row['occurred_at'],row['occurred_at'],
                     hook.quote,hook.why_funny,hook.type,hook.heat))
                db.execute('INSERT INTO profile_item_sources VALUES (?,?,?)',(cursor.lastrowid,hook.source_id,row['text_hash']))
                saved += 1
            return saved
    return await asyncio.to_thread(commit)
