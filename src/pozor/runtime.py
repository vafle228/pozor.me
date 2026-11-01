"""Telegram polling lifecycle for the durable ingestion module."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pozor.app.container import Container

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from pozor.auto_profiles import automatic_profile_loop
from pozor.commands import expire_sessions
from pozor.config import Settings
from pozor.delivery import DeliveryPipeline, health_loop, recover_delivery
from pozor.ingestion import (
    PollingConflictError,
    run_poller,
)
from pozor.privacy import cleanup
from pozor.profiles import profile_loop
from pozor.storage import connect, migrate


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


async def run_telegram(settings: Settings, container: Container | None = None) -> None:
    if container is None:
        from pozor.app.container import Container
        async with Container(settings) as owned:
            await run_telegram(settings,owned)
        return
    """Migrate storage, sync configured scope, and run one real Bot API poller."""
    bot_token = settings.bot_token
    if bot_token is None:
        raise ValueError("BOT_TOKEN is not configured")
    database_path: Path = settings.database_path
    migrate(database_path)
    recover_delivery(database_path)
    async with Bot(token=bot_token.get_secret_value()) as bot:
        client = container.client
        identity = await bot.get_me()
        await bot.set_my_commands(commands=[BotCommand(command=name, description=description)
            for name, description in (
                ("help", "Как пользоваться ботом"),
                ("roast", "Прожарить: ответьте на сообщение или укажите @username"),
                ("roast_on", "Разрешить участие и сбор своих сообщений"),
                ("roast_off", "Выключить участие"),
                ("stop", "Остановить прожарку"),
                ("finish", "Завершить сессию"),
                ("whois", "Участник: ответьте на его сообщение"),
                ("forget_me", "Удалить мои данные в этой группе"),
                ("enable_chat", "Включить чат (администратор)"),
                ("disable_chat", "Выключить чат (администратор)"),
            )], scope=BotCommandScopeAllGroupChats())
        with connect(database_path) as db:
            db.execute('INSERT OR IGNORE INTO bot_identities VALUES (?)',(identity.id,))
        source = AiogramUpdateSource(bot)
        pipeline = DeliveryPipeline(database_path,settings,bot,container.generator,container.queue)
        pipeline.settings_service = container.service

        async def cleanup_loop() -> None:
            while True:
                await asyncio.to_thread(cleanup, database_path,
                                        import_days=settings.import_retention_days)
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
        tasks: tuple[asyncio.Task[None], ...] = (cleanup_task, session_clock_task, sender_task, health_task, poller_task)
        if settings.profile_cards_enabled:
            tasks += (asyncio.create_task(profile_loop(database_path, client, pipeline.queue)),)
        if settings.semantic_profiles_enabled:
            tasks += (asyncio.create_task(automatic_profile_loop(
                database_path, settings, pipeline.queue)),)
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
