"""Claim and deliver a v0.2 reply, preserving ambiguous outcomes across restarts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class OutgoingTurn:
    session_id: str
    turn_no: int
    chat_id: int
    thread_id: int
    reply_to: int
    text: str
    deadline: float
    rating_id: int | None


@dataclass(frozen=True)
class SendResult:
    status: Literal["sent", "ready", "cancelled", "unknown"]
    error: str | None = None
    message_id: int | None = None
    retry_after: float = 0


class Outbox(Protocol):
    def claim(self, now: float) -> OutgoingTurn | Literal["cancelled"] | None: ...
    def finish(self, turn: OutgoingTurn, result: SendResult, retry_at: float) -> None: ...


class TurnTransport(Protocol):
    async def send(self, turn: OutgoingTurn, timeout: float) -> SendResult: ...


async def deliver_turn(
    outbox: Outbox, transport: TurnTransport, clock: Callable[[], float]
) -> bool:
    now = clock()
    turn = outbox.claim(now)
    if turn is None:
        return False
    if turn == "cancelled":
        return True
    try:
        # No suspension or scheduled task between the durable claim and transport call.
        result = await transport.send(turn, min(5, max(0.001, turn.deadline - now)))
    except asyncio.CancelledError:
        outbox.finish(turn, SendResult("unknown", "cancelled_http"), now)
        raise
    retry_at = clock() + max(0, result.retry_after) if result.status == "ready" else now
    outbox.finish(turn, result, retry_at)
    return True
