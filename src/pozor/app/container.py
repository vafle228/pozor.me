"""Composition root: one model queue, settings service and admin pipeline."""

import asyncio
import random
import sqlite3
from pathlib import Path
from typing import Any, Self

from aiohttp import web

from pozor.admin_pipeline import AdminPipeline
from pozor.config import Settings
from pozor.core.domain.context import ContextPack
from pozor.core.domain.generation import GenerationResult, InferenceError
from pozor.core.usecases.generate import Generator
from pozor.inference_queue import InferenceQueue
from pozor.infra.config.service import SettingsService
from pozor.infra.files.prompts import FilePromptStore
from pozor.infra.llm.client import LlamaClient
from pozor.infra.sqlite.admin_repo import AdminData
from pozor.presentation.admin.server import create_app
from pozor.storage import connect, migrate


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
            return result


class Container:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.service = SettingsService(settings)
        self.prompts = FilePromptStore()
        self.generator = RequestGenerator(self.service,self.prompts)
        self.client = LlamaClient(settings)
        self.queue = InferenceQueue(self.client.wait_idle,capacity=settings.queue_capacity,
            background_capacity=settings.background_queue_capacity, background_timeout=settings.background_timeout_seconds)
        self.admin = AdminPipeline(settings.database_path,settings,self.queue)
        self.runner: web.AppRunner | None = None

    async def __aenter__(self) -> Self:
        await asyncio.to_thread(migrate,self.settings.database_path)
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.runner:
            await self.runner.cleanup()
        await self.admin.aclose()
        await self.queue.aclose()
        await self.client.__aexit__()

    async def health(self) -> dict[str, Any]:
        def db_health(database: Path) -> str:
            with connect(database) as db:
                return str(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0])
        try:
            version = await asyncio.to_thread(db_health,self.settings.database_path)
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
