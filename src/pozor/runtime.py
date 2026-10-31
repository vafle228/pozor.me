"""Telegram polling lifecycle for the durable ingestion module."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError

from pozor.commands import expire_sessions
from pozor.config import Settings
from pozor.delivery import DeliveryPipeline, health_loop, recover_delivery
from pozor.inference_queue import InferenceQueue
from pozor.ingestion import (
    PollingConflictError,
    cleanup_expired_live_data,
    run_poller,
    sync_chat_allowlist,
)
from pozor.llm import Generator, LlamaClient
from pozor.storage import migrate


class AiogramUpdateSource:
    """Translate the real aiogram Bot API client to the ingestion source contract."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def get_updates(
        self, *, offset: int | None, timeout: int, allowed_updates: list[str]
    ) -> Sequence[Any]:
        try:
            return await self._bot.get_updates(
                offset=offset, timeout=timeout, allowed_updates=allowed_updates
            )
        except TelegramConflictError as exc:
            raise PollingConflictError(
                "Telegram getUpdates is blocked by an active webhook or another poller; "
                "remove the webhook or stop the other poller, then retry"
            ) from exc


async def run_telegram(settings: Settings) -> None:
    """Migrate storage, sync configured scope, and run one real Bot API poller."""
    bot_token = settings.bot_token
    if bot_token is None:
        raise ValueError("BOT_TOKEN is not configured")
    database_path: Path = settings.database_path
    migrate(database_path)
    sync_chat_allowlist(
        database_path, settings.chat_allowlist, retention_days=settings.live_retention_days
    )
    recover_delivery(database_path)
    async with Bot(token=bot_token.get_secret_value()) as bot, LlamaClient(settings) as client:
        identity = await bot.get_me()
        source = AiogramUpdateSource(bot)
        pipeline = DeliveryPipeline(database_path, settings, bot, Generator(client),
                                    InferenceQueue(client.wait_idle))

        async def cleanup_loop() -> None:
            while True:
                await asyncio.to_thread(cleanup_expired_live_data, database_path)
                await asyncio.sleep(3600)

        async def session_clock_loop() -> None:
            while True:
                await asyncio.to_thread(expire_sessions, database_path)
                await asyncio.sleep(5)

        async def command_handler(payload: dict[str, object]) -> None:
            await pipeline.handle_update(
                payload,
                owner_ids=frozenset(settings.owner_ids),
                bot_id=identity.id,
                bot_username=identity.username,
            )

        cleanup_task = asyncio.create_task(cleanup_loop())
        session_clock_task = asyncio.create_task(session_clock_loop())
        sender_task = asyncio.create_task(pipeline.run_sender())
        health_task = asyncio.create_task(health_loop(pipeline, client.check))
        poller_task = asyncio.create_task(
            run_poller(source, database_path, identity.id, control_handler=command_handler)
        )
        tasks = (cleanup_task, session_clock_task, sender_task, health_task, poller_task)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await pipeline.aclose()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
