"""Generate one revision of an existing v0.2 turn through the shared model queue."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from pozor.core.domain.context import ContextPack
from pozor.core.domain.generation import GenerationResult, InferenceError


@dataclass(frozen=True)
class TurnRevision:
    session_id: str
    number: int
    revision: int
    chat_id: int
    thread_id: int


class RoastGenerator(Protocol):
    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult: ...


class TurnQueue(Protocol):
    async def submit(
        self,
        *,
        key: str,
        chat_id: int,
        deadline: float,
        factory: Callable[[float], Awaitable[GenerationResult]],
    ) -> GenerationResult: ...


class TurnStore(Protocol):
    def set_deadline(self, turn: TurnRevision, deadline: float) -> None: ...
    def mark_queued(self, turn: TurnRevision, epoch: int, deadline: float) -> bool: ...
    def begin_inference(self, turn: TurnRevision, pack: ContextPack) -> bool: ...
    def roast_limit_reached(self, pack: ContextPack) -> bool: ...
    async def record(
        self,
        turn: TurnRevision,
        pack: ContextPack,
        result: GenerationResult,
    ) -> int | None: ...
    def store_result(self, turn: TurnRevision, result: GenerationResult) -> None: ...


class GenerateTurn:
    def __init__(
        self,
        store: TurnStore,
        queue: TurnQueue,
        generator: RoastGenerator,
        wall_clock: Callable[[], float],
    ):
        self.store, self.queue, self.generator, self.wall_clock = (
            store,
            queue,
            generator,
            wall_clock,
        )

    async def run(
        self,
        turn: TurnRevision,
        budget: float,
        prepare: Callable[[], Awaitable[ContextPack]],
    ) -> None:
        # One absolute deadline covers retrieval, queue wait, tokenization and validation.
        deadline = asyncio.get_running_loop().time() + budget
        wall_deadline = self.wall_clock() + budget
        self.store.set_deadline(turn, wall_deadline)
        async with asyncio.timeout_at(deadline):
            pack = await prepare()
            if not self.store.mark_queued(turn, pack.evidence_epoch, wall_deadline):
                return
            result = await self.queue.submit(
                key=f"session:{turn.session_id}",
                chat_id=turn.chat_id,
                deadline=deadline,
                factory=partial(self._generate, turn, pack),
            )
        self.store.store_result(turn, result)

    async def _generate(
        self,
        turn: TurnRevision,
        pack: ContextPack,
        deadline: float,
    ) -> GenerationResult:
        if not self.store.begin_inference(turn, pack):
            raise InferenceError("stale_before_inference")
        if pack.mode == "roast" and self.store.roast_limit_reached(pack):
            raise InferenceError("daily_round_limit")
        clock = asyncio.get_running_loop().time
        started = clock()
        result = await self.generator.generate(pack, deadline)
        result = result.model_copy(update={"latency_ms": (clock() - started) * 1000})
        generation_id = await self.store.record(turn, pack, result)
        if generation_id is None:
            raise InferenceError("stale_after_inference")
        return result.model_copy(update={"generation_id": generation_id})
