"""Offline replay through the durable inbox, legacy router, model queue and send guard."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from pozor.app.container import Container
from pozor.app.delivery import DeliveryPipeline
from pozor.config import Settings
from pozor.core.domain.context import ContextPack, Contract
from pozor.core.domain.generation import GeneratedRoast, GenerationResult
from pozor.core.usecases.generate_turn import RoastGenerator
from pozor.infra.config.registry import owner
from pozor.infra.files.deployment import Deployment
from pozor.infra.sqlite.command_store import observe
from pozor.infra.sqlite.commands import set_chat_enabled, set_consent
from pozor.infra.sqlite.delivery_state import recover_delivery
from pozor.infra.sqlite.health_repo import register_bot
from pozor.infra.sqlite.ingestion import poll_once, sync_chat_allowlist
from pozor.infra.sqlite.replay_repo import delivery_outcomes
from pozor.presentation.telegram_bot.commands import TelegramControls

CHAT = -1001
BOT = 99
USERNAME = "replay_bot"


class Message(Contract):
    kind: Literal["message", "edit"]
    at: int = Field(ge=0)
    id: int = Field(gt=0)
    author: int = Field(gt=0)
    text: str = Field(min_length=1)
    thread: int = Field(default=0, ge=0)
    reply_to: int | None = None
    reply_to_bot_for: int | None = None
    expect: Literal["silence", "reply"] | None = None


class Checkpoint(Contract):
    kind: Literal["deliver", "ambiguous", "restart"]
    at: int = Field(ge=0)


class Scenario(Contract):
    name: str = Field(min_length=1)
    events: list[Annotated[Message | Checkpoint, Field(discriminator="kind")]] = Field(min_length=1)

    @model_validator(mode="after")
    def check_sequence(self) -> Scenario:
        seen: dict[int, Message] = {}
        previous = 0
        for event in self.events:
            if event.at < previous:
                raise ValueError("scenario time must be nondecreasing")
            previous = event.at
            if not isinstance(event, Message):
                continue
            original = seen.get(event.id)
            if (event.kind == "edit") != (original is not None):
                raise ValueError("a message must be posted once before it can be edited")
            if original and (event.author, event.thread) != (original.author, original.thread):
                raise ValueError("an edit must keep its original author and topic")
            if event.author == BOT or (
                event.reply_to is not None and event.reply_to_bot_for is not None
            ):
                raise ValueError("invalid author or reply reference")
            parent = event.reply_to if event.reply_to is not None else event.reply_to_bot_for
            if parent is not None and (parent not in seen or seen[parent].thread != event.thread):
                raise ValueError("reply must reference an earlier message in this topic")
            seen[event.id] = event
        if not seen:
            raise ValueError("a replay scenario requires at least one message")
        return self


class FixtureGenerator:
    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult:
        return GenerationResult(
            roast=GeneratedRoast(
                text="Синтетический ответ для проверки доставки.",
                angle="fixture",
                evidence_ids=[pack.trigger.evidence_id],
            ),
            fallback=False,
            context_hash="fixture",
        )


async def idle(deadline: float) -> None:
    """The deterministic generator has no external slot."""


class ObservedGenerator:
    def __init__(self, generator: RoastGenerator, calls: list[dict[str, Any]]):
        self.generator = generator
        self.calls = calls

    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult:
        call: dict[str, Any] = {
            "trigger": pack.trigger.evidence_id,
            "speaker": pack.speaker_id,
            "target": pack.target_id,
            "mode": pack.mode,
            "thread": pack.thread_id,
            "recent": [
                {"id": e.source_message_id, "author": e.author_id, "thread": e.thread_id}
                for e in pack.recent
            ],
            "evidence": [{"id": e.evidence_id, "thread": e.thread_id} for e in pack.evidence],
        }
        self.calls.append(call)
        start = time.perf_counter()
        try:
            result = await self.generator.generate(pack, deadline)
            call.update(text=result.roast.text, fallback=result.fallback, reason=result.reason)
            return result
        finally:
            call["generation_ms"] = round((time.perf_counter() - start) * 1000, 1)


class Transport:
    id = BOT

    def __init__(self, first_bot_message: int) -> None:
        self.next_id = first_bot_message
        self.fail_send = False
        self.current_input: int | None = None
        self.attempts: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self.arrived: dict[int, float] = {}
        self.sent: dict[int, dict[str, Any]] = {}

    async def get_chat_member(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(status="member")

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        message_id = self.next_id
        self.next_id += 1
        reply = kwargs.get("reply_parameters")
        if reply is None:
            self.notices.append({"input": self.current_input, "text": kwargs["text"]})
        else:
            trigger = reply.message_id
            self.attempts.append(
                {
                    "reply_to": trigger,
                    "thread": kwargs["message_thread_id"] or 0,
                    "text": kwargs["text"],
                    "outcome": "unknown" if self.fail_send else "sent",
                    "arrival_to_attempt_ms": round(
                        (time.perf_counter() - self.arrived[trigger]) * 1000
                    ),
                }
            )
            if self.fail_send:
                raise TimeoutError
            self.sent[trigger] = {
                "message_id": message_id,
                "text": kwargs["text"],
                "from": {"id": BOT, "is_bot": True, "first_name": "Бот"},
            }
        return SimpleNamespace(message_id=message_id)


class Batch:
    def __init__(self, update: dict[str, Any]) -> None:
        self.update = update

    async def get_updates(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [self.update]


class GroupReplay:
    def __init__(self, directory: Path, scenario: Scenario, live: bool, port: int):
        self.scenario = scenario
        self.live = live
        # Only registry defaults and these explicit inputs are used. Ignore inherited secrets,
        # .env, database overrides and model endpoints from the active installation.
        self.settings = Settings.model_construct(
            pozor_data_dir=directory,
            database_path=directory / "replay.sqlite3",
            llm_base_url=f"http://127.0.0.1:{port}",
        )
        self.database = self.settings.database_path
        self.start = int(time.time())
        self.now = self.start
        self.messages: dict[int, dict[str, Any]] = {}
        self.generations: list[dict[str, Any]] = []
        self.unavailable: list[int] = []
        self.bot = Transport(1 + max(e.id for e in scenario.events if isinstance(e, Message)))

    @asynccontextmanager
    async def runtime(self) -> AsyncIterator[DeliveryPipeline]:
        async with Container(self.settings) as container:
            if self.live:
                await container.client.check(asyncio.get_running_loop().time() + 5)
            else:
                container.queue.wait_idle = idle
            recover_delivery(self.database)
            pipeline = DeliveryPipeline(
                self.database,
                self.settings,
                self.bot,
                ObservedGenerator(
                    container.generator if self.live else FixtureGenerator(), self.generations
                ),
                container.queue,
                clock=lambda: self.now,
            )
            pipeline.settings_service = container.service
            controls = TelegramControls(
                self.database,
                owner_ids=frozenset(),
                bot_id=BOT,
                bot_username=USERNAME,
                settings=self.settings,
            )
            pipeline.control_handler = controls
            pipeline.ready = True
            try:
                yield pipeline
            finally:
                await pipeline.stop_jobs()
                await controls.aclose()

    def seed(self) -> None:
        register_bot(self.database, BOT)
        sync_chat_allowlist(self.database, [CHAT])
        set_chat_enabled(self.database, CHAT, True)
        for author in {e.author for e in self.scenario.events if isinstance(e, Message)}:
            observe(self.database, CHAT, author, f"Участник {author}", None)
            set_consent(self.database, CHAT, author, True)

    async def message(self, event: Message, update_id: int, pipeline: DeliveryPipeline) -> None:
        raw: dict[str, Any] = {
            "chat": {"id": CHAT, "type": "supergroup"},
            "from": {"id": event.author, "first_name": f"Участник {event.author}", "is_bot": False},
            "message_id": event.id,
            "date": self.now,
            "text": event.text,
        }
        if event.thread:
            raw.update(message_thread_id=event.thread, is_topic_message=True)
        if event.text.startswith("/"):
            raw["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(event.text.split()[0])}
            ]
        elif event.text.startswith("@" + USERNAME):
            raw["entities"] = [{"type": "mention", "offset": 0, "length": 1 + len(USERNAME)}]
        if event.reply_to is not None:
            if event.reply_to not in self.messages:
                self.unavailable.append(event.id)
                return
            raw["reply_to_message"] = self.messages[event.reply_to]
        if event.reply_to_bot_for is not None:
            parent = self.bot.sent.get(event.reply_to_bot_for)
            if parent is None:
                self.unavailable.append(event.id)
                return
            raw["reply_to_message"] = parent | {"chat": raw["chat"], "date": self.now}
        self.messages[event.id] = {k: v for k, v in raw.items() if k != "reply_to_message"}
        field = "edited_message" if event.kind == "edit" else "message"
        self.bot.arrived.setdefault(event.id, time.perf_counter())
        self.bot.current_input = event.id

        async def route(payload: dict[str, object]) -> None:
            await pipeline.handle_update(
                payload, owner_ids=frozenset(), bot_id=BOT, bot_username=USERNAME
            )

        await poll_once(
            Batch({"update_id": update_id, field: raw}), self.database, BOT, control_handler=route
        )
        await asyncio.gather(*list(pipeline.jobs.values()))
        self.bot.current_input = None

    async def run(self) -> dict[str, Any]:
        events = iter(enumerate(self.scenario.events, 1))
        first_start = True
        while True:
            async with self.runtime() as pipeline:
                if first_start:
                    self.seed()
                    first_start = False
                for index, event in events:
                    self.now = self.start + event.at
                    if isinstance(event, Message):
                        await self.message(event, index, pipeline)
                    elif event.kind == "restart":
                        break
                    else:
                        self.bot.fail_send = event.kind == "ambiguous"
                        pipeline.invalidate()
                        while await pipeline.send_once():
                            pass
                        self.bot.fail_send = False
                else:
                    break
        desired = {
            e.id for e in self.scenario.events if isinstance(e, Message) and e.expect == "reply"
        }
        quiet = {
            e.id for e in self.scenario.events if isinstance(e, Message) and e.expect == "silence"
        }
        intervened = {a["reply_to"] for a in self.bot.attempts} | {
            n["input"] for n in self.bot.notices
        }
        return {
            "scenario": self.scenario.name,
            "expected_reply_to": sorted(desired),
            "expected_silence": sorted(quiet),
            "missed_replies": sorted(desired - self.bot.sent.keys() - set(self.unavailable)),
            "inputs_without_parent": self.unavailable,
            "unwanted_interventions": sorted(quiet & intervened),
            "attempts": self.bot.attempts,
            "generations": self.generations,
            "notices": self.bot.notices,
            "turns": delivery_outcomes(self.database),
        }


async def replay(suite: Path, *, live: bool, port: int) -> dict[str, Any]:
    contents = suite.read_bytes()
    scenarios = TypeAdapter(list[Scenario]).validate_json(contents)
    if not scenarios or len({s.name for s in scenarios}) != len(scenarios):
        raise ValueError("a replay suite requires distinct named scenarios")
    deployment = Deployment.model_validate_json(Path("deployment-lock.json").read_bytes())
    source = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".sql", ".txt", ".json", ".jinja", ".md"}:
            digest.update(path.relative_to(source).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    results = []
    with TemporaryDirectory(prefix="pozor-replay-") as tmp:
        for index, scenario in enumerate(scenarios):
            driver = GroupReplay(Path(tmp) / str(index), scenario, live, port)
            results.append(await driver.run())
        settings = driver.settings.model_dump(mode="json")
    return {
        "schema_version": 1,
        "mode": "local_model" if live else "fixture_generator",
        "suite_sha256": hashlib.sha256(contents).hexdigest(),
        "source_sha256": digest.hexdigest(),
        "declared_deployment": deployment.model_dump(),
        "transport": "in_memory",
        "schedule": "generation settles after each input; delivery and restart at explicit checkpoints",
        "llm_alias": settings["llm_model"],
        "context_size": settings["context_size"],
        "runtime_settings": {k: v for k, v in settings.items() if owner(k) == "runtime"},
        "human_quality": "not_evaluated",
        "scenarios": results,
    }
