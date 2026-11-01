"""Unified lifecycle for serve and admin-only modes."""

import asyncio
from typing import Any

from pozor.app.container import Container
from pozor.config import Settings


async def serve(settings: Settings, *, admin_only: bool = False, overrides: dict[str,Any] | None = None) -> None:
    from pozor.app.telegram_runtime import run_telegram
    from pozor.infra.logging import configure_logging

    configure_logging(settings)
    async with Container(settings,overrides) as container:
        await container.start_admin()
        if admin_only:
            await asyncio.Event().wait()
        else:
            await run_telegram(settings,container)
