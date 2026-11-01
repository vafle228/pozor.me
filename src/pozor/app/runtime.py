"""Unified lifecycle for serve and admin-only modes."""

import asyncio

from pozor.app.container import Container
from pozor.config import Settings


async def serve(settings: Settings, *, admin_only: bool = False) -> None:
    from pozor.runtime import run_telegram

    async with Container(settings) as container:
        await container.start_admin()
        if admin_only:
            await asyncio.Event().wait()
        else:
            await run_telegram(settings,container)
