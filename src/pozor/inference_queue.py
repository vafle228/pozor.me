"""One GPU worker, bounded per-session admission and round-robin chat fairness.

Factories receive the original monotonic deadline. Callers own revision/epoch
checks (T5); successfully cancelling HTTP never authorizes publishing a result.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class QueueBusy(RuntimeError):
    pass


@dataclass
class _Job:
    key: str
    chat: int
    deadline: float
    factory: Callable[[float], Awaitable[Any]]
    future: asyncio.Future[Any]
    background: bool


class InferenceQueue:
    def __init__(
        self, wait_idle: Callable[[float], Awaitable[None]], *,
        capacity: int = 8, background_capacity: int = 16,
    ):
        if min(capacity, background_capacity) < 1:
            raise ValueError("queue capacity must be positive")
        self.capacity, self.background_capacity = capacity, background_capacity
        self.wait_idle = wait_idle
        self.chats: OrderedDict[int, deque[_Job]] = OrderedDict()
        self.background: OrderedDict[str, _Job] = OrderedDict()
        self.keys: set[str] = set()
        self.wake = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None
        self.active: _Job | None = None
        self.operation: asyncio.Task[Any] | None = None
        self.closed = False

    async def submit(
        self, *, key: str, chat_id: int, factory: Callable[[float], Awaitable[Any]],
        deadline: float, background: bool = False,
    ) -> Any:
        if self.closed:
            raise QueueBusy("queue_closed")
        now = asyncio.get_running_loop().time()
        if deadline <= now:
            raise TimeoutError
        self._prune()
        if key in self.keys:
            if background and key in self.background:
                old = self.background.pop(key)
                old.future.cancel()
                self.keys.remove(key)
            else:
                raise QueueBusy("session_already_pending")
        if background:
            if len(self.background) >= self.background_capacity:
                raise QueueBusy("background_full")
            deadline = min(deadline, now + 5)
        elif sum(len(q) for q in self.chats.values()) >= self.capacity:
            raise QueueBusy("interactive_full")
        job = _Job(key, chat_id, deadline, factory, asyncio.get_running_loop().create_future(),
                   background)
        self.keys.add(key)
        if background:
            self.background[key] = job
        else:
            self.chats.setdefault(chat_id, deque()).append(job)
            if self.active and self.active.background and self.operation:
                self.operation.cancel()
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())
        self.wake.set()
        try:
            async with asyncio.timeout_at(deadline):
                return await job.future
        finally:
            if not job.future.done():
                job.future.cancel()
            if self.active is job and self.operation:
                self.operation.cancel()
            self._prune()

    def _prune(self) -> None:
        now = asyncio.get_running_loop().time()
        for chat, queue in list(self.chats.items()):
            kept = deque(self._live(job, now) for job in queue)
            self.chats[chat] = deque(job for job in kept if job is not None)
            if not self.chats[chat]:
                del self.chats[chat]
        for key, job in list(self.background.items()):
            if self._live(job, now) is None:
                del self.background[key]

    def _live(self, job: _Job, now: float) -> _Job | None:
        if job.future.done() or job.deadline <= now:
            if not job.future.done():
                job.future.set_exception(TimeoutError())
            self.keys.discard(job.key)
            return None
        return job

    def cancel(self, key: str) -> None:
        for job in [j for q in self.chats.values() for j in q] + list(self.background.values()):
            if job.key == key:
                job.future.cancel()
        if self.active and self.active.key == key:
            self.active.future.cancel()
            if self.operation:
                self.operation.cancel()
        self._prune()

    async def _execute(self, job: _Job) -> Any:
        async with asyncio.timeout_at(job.deadline):
            await self.wait_idle(job.deadline)
            return await job.factory(job.deadline)

    async def _run(self) -> None:
        while not self.closed:
            self._prune()
            job = None
            if self.chats:
                chat, queue = self.chats.popitem(last=False)
                job = queue.popleft()
                if queue:
                    self.chats[chat] = queue
            elif self.background:
                _, job = self.background.popitem(last=False)
            if job is None:
                self.wake.clear()
                await self.wake.wait()
                continue
            self.active = job
            self.operation = asyncio.create_task(self._execute(job))
            try:
                result = await self.operation
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                job.future.cancel()
            except Exception as exc:  # noqa: BLE001 -- deliver job failure; keep worker alive
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                self.keys.discard(job.key)
                self.active, self.operation = None, None

    async def aclose(self) -> None:
        self.closed = True
        for key in list(self.keys):
            self.cancel(key)
        self.wake.set()
        if self.worker:
            await self.worker
