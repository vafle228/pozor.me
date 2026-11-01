"""Automatic quote-backed semantic portraits on the preemptible inference queue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from functools import partial
from pathlib import Path

from pozor.config import Settings
from pozor.infra.config.service import SettingsService
from pozor.infra.llm.queue import InferenceQueue, QueueBusy
from pozor.infra.sqlite import automatic_repo
from pozor.infra.sqlite import profile_repo as semantic_profiles
from pozor.infra.telegram_account import AccountError
from pozor.llm import InferenceError

logger = logging.getLogger(__name__)


async def refresh(database: Path, settings: Settings, queue: InferenceQueue,
                  chat: int, target: int) -> bool:
    version, rows = await asyncio.to_thread(semantic_profiles.snapshot, database, chat, target)
    signature = hashlib.sha256(repr((version, [(r['id'], r['text_hash']) for r in rows])).encode()).hexdigest()
    if not await asyncio.to_thread(automatic_repo.begin,database,chat,target,rows,signature):
        return False
    progress = partial(automatic_repo.progress,database,chat,target)

    try:
        draft = await semantic_profiles.analyze(database, settings, chat, target, progress,
                                                 queue=queue, cumulative=True)
        # Use the exact snapshot analyzed, including concurrent new arrivals.
        selections = [{'index': i, 'summary': item['summary']}
                      for i, item in enumerate(draft['observations'])]
        await asyncio.to_thread(semantic_profiles.save,database,draft,selections,automatic=True)
        retained = await asyncio.to_thread(semantic_profiles.retained_observations,database,chat,target)
        await asyncio.to_thread(automatic_repo.finish,database,chat,target,draft,bool(selections or retained))
        from pozor.infra.sqlite.hook_repo import refresh_hooks
        if settings.humor_v2_enabled:
            await refresh_hooks(database,settings,queue,chat,target)
        logger.info('Automatic portrait complete: sampled=%s observations=%s', draft['sampled'], len(selections))
        return True
    except asyncio.CancelledError:
        await asyncio.to_thread(automatic_repo.state,database,chat,target,'pending')
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        return False  # Interactive generation preempted this background batch.
    except (InferenceError, QueueBusy, TimeoutError, AccountError) as exc:
        await asyncio.to_thread(automatic_repo.state,database,chat,target,'error',type(exc).__name__)
        logger.warning('Automatic portrait deferred: %s', type(exc).__name__)
        return False


async def automatic_profile_loop(database: Path, settings: Settings, queue: InferenceQueue, service: SettingsService | None = None) -> None:
    while True:
        try:
            if service:
                settings=await asyncio.to_thread(service.snapshot)
            if not settings.semantic_profiles_enabled:
                await asyncio.sleep(30)
                continue
            targets=await asyncio.to_thread(automatic_repo.targets,database)
            for row in targets:
                try:
                    await refresh(database, settings, queue, row['chat_id'], row['user_id'])
                except (AccountError, sqlite3.Error):
                    logger.warning('Automatic portrait scope changed or storage busy')
        except sqlite3.Error:
            logger.exception('Automatic portrait storage unavailable')
        await asyncio.sleep(30)
