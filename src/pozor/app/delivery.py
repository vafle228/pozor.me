"""Own the legacy conversation workers, backlog and cancellation in the application loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError

from pozor.config import Settings
from pozor.core.domain.generation import InferenceError
from pozor.core.usecases.deliver_turn import deliver_turn
from pozor.core.usecases.generate_turn import GenerateTurn, RoastGenerator
from pozor.infra import telegram_events
from pozor.infra.config.service import SettingsService
from pozor.infra.llm.queue import InferenceQueue, QueueBusy
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.sqlite.turn_generation import TurnGenerationRepository
from pozor.infra.sqlite.turn_intake import TurnIntake
from pozor.infra.sqlite.turn_outbox import TurnOutbox
from pozor.infra.turn_transport import TelegramTurnTransport

logger = logging.getLogger(__name__)


class DeliveryPipeline:
    def __init__(
        self,
        database: Path,
        settings: Settings,
        bot: Any,
        generator: RoastGenerator,
        queue: InferenceQueue,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database, self.settings, self.bot = database, settings, bot
        self.generator, self.queue, self.clock = generator, queue, clock
        self.intake = TurnIntake(database, clock)
        self.outbox = TurnOutbox(database, clock)
        self.transport = TelegramTurnTransport(bot)
        self.generation_log = GenerationRepository(database)
        self.settings_service: SettingsService | None = None
        self.control_handler: Callable[..., Awaitable[None]] | None = None
        self.ready = False
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.due: dict[str, tuple[float, float]] = {}
        self.rebuilds: dict[str, int] = {}
        self.notice_at: dict[int, float] = {}
        self.stopping = False

    async def notify(self, chat_id: int, thread_id: int, text: str) -> None:
        # Shared cooldown for queue-full and health notices. No archive/prompt in logs.
        now = self.clock()
        if not self.intake.chat_enabled(chat_id):
            return
        if now < self.notice_at.get(chat_id, 0):
            return
        self.notice_at[chat_id] = now + 30
        try:
            async with asyncio.timeout(2):
                await self.bot.send_message(
                    chat_id=chat_id, message_thread_id=thread_id or None, text=text, parse_mode=None
                )
        except (TelegramAPIError, OSError, TimeoutError):
            logger.warning("delivery_notice_failed chat_id=%s", chat_id)

    def accept(
        self,
        session_id: str,
        message_id: int,
        text: str,
        *,
        first: bool = False,
        author_id: int | None = None,
    ) -> bool:
        """Persist/coalesce one turn; never wait for the GPU in the update handler."""
        if self.stopping or not self.ready:
            return False
        now = self.clock()
        admission = self.intake.accept(
            session_id, message_id, text, author_id, self.rebuilds.get(session_id, 0)
        )
        if admission is None:
            return False
        self.rebuilds[session_id] = admission.rebuilds
        if not admission.accepted:
            self.queue.cancel(f"session:{session_id}")
            return False
        loop = asyncio.get_running_loop()
        began = self.due.get(session_id, (loop.time(), 0))[0]
        due = loop.time() if first else min(loop.time() + 1.2, began + 3)
        # Fast replies wait for the ten-second product cooldown, not a new round.
        if admission.last_sent_at is not None:
            due = max(due, loop.time() + max(0, admission.last_sent_at + 10 - now))
        self.due[session_id] = (began, due)
        self.queue.cancel(f"session:{session_id}")
        if session_id not in self.jobs or self.jobs[session_id].done():
            self.rebuilds[session_id] = 0
            self.jobs[session_id] = asyncio.create_task(self._drive(session_id))
            self.jobs[session_id].add_done_callback(lambda _: self.jobs.pop(session_id, None))
        return True

    def _generation_settings(self) -> Settings:
        return self.settings_service.snapshot() if self.settings_service else self.settings

    async def _drive(self, session_id: str) -> None:
        store = TurnGenerationRepository(
            self.database, self.generation_log, self._generation_settings, self.clock
        )
        generation = GenerateTurn(store, self.queue, self.generator, self.clock)
        typing: asyncio.Task[None] | None = None
        try:
            if hasattr(self.bot, "send_chat_action"):
                scope = store.scope(session_id)
                if scope:
                    typing = asyncio.create_task(self._typing(*scope))
            while not self.stopping:
                settings = (
                    await asyncio.to_thread(self.settings_service.snapshot)
                    if self.settings_service
                    else self.settings
                )
                delay = self.due[session_id][1] - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(min(delay, 0.1))
                    continue
                pending = store.pending(session_id)
                if pending is None:
                    return
                turn = pending.turn
                try:
                    await generation.run(
                        turn,
                        settings.generation_deadline_seconds,
                        partial(store.context, pending, settings),
                    )
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if self.stopping or (task and task.cancelling()):
                        raise
                except (TimeoutError, InferenceError, QueueBusy) as exc:
                    reason = "deadline" if isinstance(exc, TimeoutError) else str(exc)
                    if reason.startswith("stale_"):
                        return
                    logger.warning("generation_failed reason=%s", reason)
                    if isinstance(exc, QueueBusy):
                        await self.notify(
                            turn.chat_id, turn.thread_id, "Сейчас занят. Попробуйте позже."
                        )
                    store.fail(turn, reason)
                    if not isinstance(exc, QueueBusy):
                        await self.notify(
                            turn.chat_id,
                            turn.thread_id,
                            "Ответ не успел подготовиться. Раунд сохранён; можно продолжить тегом или ответом.",
                        )
                if not store.has_pending(session_id):
                    return
        except Exception as exc:  # noqa: BLE001 -- retain private diagnostics by type only
            logger.error(
                "generation_job_failed session_id=%s type=%s", session_id, type(exc).__name__
            )
            store.fail_job(session_id, "job_error:" + type(exc).__name__)
        finally:
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
            self.due.pop(session_id, None)
            self.rebuilds.pop(session_id, None)

    async def _typing(self, chat_id: int, thread_id: int) -> None:
        while True:
            try:
                async with asyncio.timeout(2):
                    await self.bot.send_chat_action(
                        chat_id=chat_id, message_thread_id=thread_id or None, action="typing"
                    )
            except (TelegramAPIError, OSError, TimeoutError):
                pass
            await asyncio.sleep(4)

    def invalidate(self) -> None:
        """Cheap periodic guards also notice edits/rollback from the local import CLI."""
        for key in self.intake.invalidate(self.queue.keys, self.jobs.keys()):
            self.queue.cancel(key)

    async def send_once(self) -> bool:
        return await deliver_turn(self.outbox, self.transport, self.clock)

    async def run_sender(self) -> None:
        while not self.stopping:
            self.invalidate()
            await self.send_once()
            await asyncio.sleep(0.1)

    async def handle_update(
        self,
        payload: dict[str, object],
        *,
        bot_id: int,
        bot_username: str | None,
        owner_ids: frozenset[int],
    ) -> None:
        await telegram_events.handle_update(
            self,
            payload,
            bot_id=bot_id,
            bot_username=bot_username,
            owner_ids=owner_ids,
        )

    async def handle_message(self, payload: dict[str, object], bot_username: str | None) -> None:
        await telegram_events.handle_message(self, payload, bot_username)

    async def stop_jobs(self) -> None:
        self.stopping = True
        for sid, task in self.jobs.items():
            self.queue.cancel(f"session:{sid}")
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)

    async def aclose(self) -> None:
        """Close a standalone pipeline and its queue."""
        await self.stop_jobs()
        await self.queue.aclose()


async def health_loop(
    pipeline: DeliveryPipeline, check: Callable[[float], Awaitable[None]]
) -> None:
    while not pipeline.stopping:
        try:
            await check(asyncio.get_running_loop().time() + 3)
            pipeline.ready = True
        except (InferenceError, TimeoutError):
            pipeline.ready = False
        await asyncio.sleep(5)
