"""Automatic quote-backed semantic portraits on the preemptible inference queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from pathlib import Path

from pozor import semantic_profiles
from pozor.config import Settings
from pozor.inference_queue import InferenceQueue, QueueBusy
from pozor.llm import InferenceError
from pozor.storage import connect
from pozor.telegram_account import AccountError

logger = logging.getLogger(__name__)


async def refresh(database: Path, settings: Settings, queue: InferenceQueue,
                  chat: int, target: int) -> bool:
    version, rows = await asyncio.to_thread(semantic_profiles.snapshot, database, chat, target)
    signature = hashlib.sha256(repr((version, [(r['id'], r['text_hash']) for r in rows])).encode()).hexdigest()
    now = time.time()
    with connect(database) as db:
        old = db.execute('SELECT * FROM automatic_profiles WHERE chat_id=? AND target_id=?',
                         (chat, target)).fetchone()
        has_dossier = db.execute('SELECT 1 FROM profile_cards WHERE chat_id=? AND target_id=? '
                                 'AND profile_version=4', (chat, target)).fetchone()
        if (old and old['signature'] == signature
                and (old['state'] in {'empty', 'insufficient'} or old['state'] == 'ready' and has_dossier)):
            return False
        if old and now - old['attempted_at'] < 60:
            return False
        # Refresh after 20 new own messages or ten minutes from the last build.
        if old and old['signature'] and old['state'] in {'ready', 'empty'}:
            new = sum(r['id'] > old['message_id'] for r in rows)
            if new < 20 and now - old['updated_at'] < 600:
                return False
        db.execute('''INSERT INTO automatic_profiles(chat_id,target_id) VALUES (?,?)
            ON CONFLICT DO NOTHING''', (chat, target))
        db.execute('''UPDATE automatic_profiles SET state=?,attempted_at=?,analyzed=0,
            sampled=?,error_code=NULL WHERE chat_id=? AND target_id=?''',
                   ('running' if len(rows) >= 10 else 'insufficient', now, len(rows), chat, target))
        if len(rows) < 10:
            db.execute('UPDATE automatic_profiles SET signature=? WHERE chat_id=? AND target_id=?',
                       (signature, chat, target))
            return False

    def progress(done: int, total: int) -> None:
        with connect(database) as db:
            db.execute('UPDATE automatic_profiles SET analyzed=?,sampled=? WHERE chat_id=? AND target_id=?',
                       (done, total, chat, target))

    try:
        draft = await semantic_profiles.analyze(database, settings, chat, target, progress,
                                                 queue=queue, cumulative=True)
        # Use the exact snapshot analyzed, including concurrent new arrivals.
        actual_signature = draft['signature']
        selections = [{'index': i, 'summary': item['summary']}
                      for i, item in enumerate(draft['observations'])]
        with connect(database) as db:
            db.execute('BEGIN IMMEDIATE')
            semantic_profiles.validate_in_transaction(db, draft)
        semantic_profiles.save(database, draft, selections, automatic=True)
        with connect(database) as db:
            db.execute('''UPDATE automatic_profiles SET signature=?,message_id=?,updated_at=?,
                state=?,analyzed=sampled WHERE chat_id=? AND target_id=?''',
                       (actual_signature, draft['message_id'], time.time(),
                        'ready' if selections or semantic_profiles.retained_observations(database, chat, target)
                        else 'empty', chat, target))
        logger.info('Automatic portrait complete: sampled=%s observations=%s', draft['sampled'], len(selections))
        return True
    except asyncio.CancelledError:
        with connect(database) as db:
            db.execute("UPDATE automatic_profiles SET state='pending' WHERE chat_id=? AND target_id=?",
                       (chat, target))
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        return False  # Interactive generation preempted this background batch.
    except (InferenceError, QueueBusy, TimeoutError, AccountError) as exc:
        with connect(database) as db:
            db.execute("UPDATE automatic_profiles SET state='error',error_code=? WHERE chat_id=? AND target_id=?",
                       (type(exc).__name__, chat, target))
        logger.warning('Automatic portrait deferred: %s', type(exc).__name__)
        return False


async def automatic_profile_loop(database: Path, settings: Settings, queue: InferenceQueue) -> None:
    while True:
        try:
            with connect(database) as db:
                targets = db.execute('''SELECT m.chat_id,m.user_id FROM members m JOIN chats c USING(chat_id)
                    WHERE m.consent=1 AND m.opt_out=0 AND c.allowed=1 AND c.enabled=1
                    AND NOT EXISTS (SELECT 1 FROM forgotten_members f
                        WHERE f.chat_id=m.chat_id AND f.user_id=m.user_id)
                    ORDER BY (SELECT count(*) FROM messages msg
                        WHERE msg.scope_chat_id=m.chat_id AND msg.author_id=m.user_id) DESC''').fetchall()
            for row in targets:
                try:
                    await refresh(database, settings, queue, row['chat_id'], row['user_id'])
                except (AccountError, sqlite3.Error):
                    logger.warning('Automatic portrait scope changed or storage busy')
        except sqlite3.Error:
            logger.exception('Automatic portrait storage unavailable')
        await asyncio.sleep(30)
