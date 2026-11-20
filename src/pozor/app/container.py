"""Composition root: one model queue, settings service and admin pipeline."""

import asyncio
import random
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Self

from aiohttp import web

from pozor.config import Settings
from pozor.core.domain.context import ContextPack
from pozor.core.domain.generation import GenerationResult, InferenceError
from pozor.core.usecases.generate import Generator
from pozor.infra.config.service import SettingsService
from pozor.infra.files.process_lock import ProcessLock
from pozor.infra.files.prompts import FilePromptStore
from pozor.infra.llm.client import LlamaClient
from pozor.infra.llm.queue import InferenceQueue
from pozor.infra.sqlite.admin_pipeline import AdminPipeline
from pozor.infra.sqlite.admin_repo import AdminData
from pozor.infra.sqlite.connection import migrate
from pozor.presentation.admin.server import create_app


def _model_queue(settings: Settings, client: LlamaClient) -> InferenceQueue:
    return InferenceQueue(
        client.wait_idle,
        capacity=settings.queue_capacity,
        background_capacity=settings.background_queue_capacity,
        background_timeout=settings.background_timeout_seconds,
    )


class DiagnosticBusy(RuntimeError):
    pass


@asynccontextmanager
async def standalone_inference(settings: Settings) -> AsyncIterator[tuple[LlamaClient, InferenceQueue]]:
    """Own this installation during a model diagnostic without opening its database."""
    lock = ProcessLock(settings.database_path)
    try:
        lock.acquire()
    except ValueError:
        raise DiagnosticBusy("Установка занята другим процессом Pozor.") from None
    try:
        async with LlamaClient(settings) as client:
            queue = _model_queue(settings, client)
            try:
                yield client, queue
            finally:
                await queue.aclose()
    finally:
        lock.release()


@dataclass(frozen=True)
class DiagnosticGeneration:
    result: GenerationResult
    queue_wait_ms: float
    generation_ms: float | None


async def diagnostic_generation(
    queue: InferenceQueue, generator: Generator, pack: ContextPack, deadline: float,
) -> DiagnosticGeneration:
    submitted = time.perf_counter()
    started: float | None = None

    async def generate(job_deadline: float) -> GenerationResult:
        nonlocal started
        started = time.perf_counter()
        return await generator.generate(pack, job_deadline)

    try:
        result: GenerationResult = await queue.submit(
            key=f"diagnostic:{pack.trigger.evidence_id}",
            chat_id=pack.chat_id,
            factory=generate,
            deadline=deadline,
        )
    except (InferenceError, TimeoutError) as exc:
        # The slot check can fail before the generator starts. Keep the same report and breaker.
        circuit_open = asyncio.get_running_loop().time() < generator.open_until
        result = generator.fallback_result(pack, InferenceError("circuit_open") if circuit_open else exc)
    finished = time.perf_counter()
    if started is None:
        return DiagnosticGeneration(result, (finished - submitted) * 1000, None)
    return DiagnosticGeneration(result, (started - submitted) * 1000, (finished - started) * 1000)


class RequestGenerator:
    def __init__(self, service: SettingsService, prompts: FilePromptStore):
        self.service, self.prompts = service, prompts
        self.failures = 0
        self.open_until = 0.0

    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult:
        snapshot = await asyncio.to_thread(self.service.snapshot)
        async with LlamaClient(snapshot) as client:
            generator = Generator(client, random.SystemRandom(), self.prompts)
            generator.failures, generator.open_until = self.failures, self.open_until
            result = await generator.generate(pack, deadline)
            self.failures, self.open_until = generator.failures, generator.open_until
            return result.model_copy(update={'model_name':snapshot.llm_model,'sampling_params':{
                name:getattr(snapshot,name) for name in ('temperature','top_p','min_p','presence_penalty',
                    'max_output_tokens','generation_deadline_seconds')}})


class Container:
    def __init__(self, settings: Settings, overrides: dict[str,Any] | None = None):
        self.settings = settings
        self.process_lock = ProcessLock(settings.database_path)
        self.service = SettingsService(settings,cli=overrides)
        self.prompts = FilePromptStore()
        self.generator = RequestGenerator(self.service,self.prompts)
        self.client = LlamaClient(settings)
        self.queue = _model_queue(settings, self.client)
        self.admin = AdminPipeline(settings.database_path,settings,self.queue)
        self.runner: web.AppRunner | None = None
        self.settings_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.process_lock.acquire()
        try:
            await asyncio.to_thread(migrate,self.settings.database_path)
            await asyncio.to_thread(self.service.snapshot)
        except BaseException:
            try:
                await self.client.__aexit__()
            finally:
                self.process_lock.release()
            raise
        await self.client.__aenter__()
        self.settings_task=asyncio.create_task(self.apply_settings())
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            if self.settings_task:
                self.settings_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.settings_task
            if self.runner:
                await self.runner.cleanup()
            await self.admin.aclose()
            await self.queue.aclose()
            await self.client.__aexit__()
        finally:
            self.process_lock.release()

    async def apply_settings(self) -> None:
        while True:
            snapshot=await asyncio.to_thread(self.service.snapshot)
            self.queue.capacity=snapshot.queue_capacity
            self.queue.background_capacity=snapshot.background_queue_capacity
            self.queue.background_timeout=snapshot.background_timeout_seconds
            self.admin.settings=snapshot
            await asyncio.sleep(.5)

    async def health(self) -> dict[str, Any]:
        from pozor.infra.sqlite.health_repo import schema
        try:
            version = await asyncio.to_thread(schema,self.settings.database_path)
            await self.client.check(asyncio.get_running_loop().time()+2)
        except (sqlite3.Error,InferenceError,TimeoutError):
            return {'ok':False,'status':'dependency_unavailable'}
        return {'ok':True,'database':'ok','llm':'ok','schema_version':version,
                'queue':{'pending':len(self.queue.keys),'active':self.queue.active is not None}}

    async def start_admin(self) -> None:
        if self.settings.admin_password is None:
            raise ValueError('ADMIN_PASSWORD is not configured (minimum 16 characters)')
        self.runner = web.AppRunner(create_app(AdminData(self.settings.database_path),
            self.settings.admin_password.get_secret_value(), settings=self.settings,
            pipeline=self.admin,service=self.service,health=self.health), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner,self.settings.admin_host,self.settings.admin_port).start()
