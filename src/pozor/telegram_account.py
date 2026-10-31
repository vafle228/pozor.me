"""Optional user-account reader. All methods run on the admin's dedicated event loop."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import qrcode
import qrcode.image.svg
from telethon import TelegramClient, errors, utils

from pozor.config import Settings


class AccountError(ValueError):
    """Safe, user-facing error, without RPC details or account credentials."""


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


class TelegramAccount:
    def __init__(self, settings: Settings, directory: Path):
        self.settings = settings
        self.directory = directory
        self.client: Any = None
        self.login_task: asyncio.Task[None] | None = None
        self.state = "disconnected"
        self.qr: str | None = None
        self.entities: dict[int, Any] = {}

    async def open(self) -> None:
        if self.client is not None:
            return
        api_id = self.settings.telegram_api_id
        api_hash = (self.settings.telegram_api_hash.get_secret_value()
                    if self.settings.telegram_api_hash else None)
        credentials = self.directory / "application.json"
        if not (api_id and api_hash) and credentials.exists():
            saved = json.loads(credentials.read_text("utf-8"))
            api_id, api_hash = saved["api_id"], saved["api_hash"]
        if not (api_id and api_hash):
            raise AccountError("Укажите API ID и API Hash приложения Telegram.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = TelegramClient(str(self.directory / "account"), api_id, api_hash,
                                     receive_updates=False, flood_sleep_threshold=0,
                                     request_retries=1, connection_retries=1)
        try:
            await self.client.connect()
            if await self.client.is_user_authorized():
                self.state = "connected"
        except BaseException:
            await self.client.disconnect()
            self.client = None
            raise

    async def status(self) -> dict[str, Any]:
        if self.client is None:
            try:
                await self.open()
            except AccountError:
                pass
        return {"state": self.state, "qr": self.qr,
                "configured": bool(self.settings.telegram_api_id and self.settings.telegram_api_hash)
                or (self.directory / "application.json").exists()}

    async def start(self, api_id: int | None = None, api_hash: str | None = None) -> dict[str, Any]:
        if self.state == "connected":
            return await self.status()
        if api_id is not None or api_hash:
            if (type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str)
                    or len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash)):
                raise AccountError("Проверьте API ID и API Hash.")
            await self.close()
            private_write(self.directory / "application.json",
                          json.dumps({"api_id": api_id, "api_hash": api_hash}))
        await self.open()
        if self.state == "connected":
            return await self.status()
        await self.cancel_login()
        login = await self.client.qr_login()
        output = BytesIO()
        qrcode.make(login.url, image_factory=qrcode.image.svg.SvgPathImage).save(output)
        self.qr = "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode()
        self.state = "qr"

        async def wait() -> None:
            try:
                await login.wait(timeout=90)
                self.state = "connected"
            except errors.SessionPasswordNeededError:
                self.state = "password"
            except (TimeoutError, errors.RPCError, OSError):
                self.state = "expired"
            finally:
                self.qr = None

        self.login_task = asyncio.create_task(wait())
        return await self.status()

    async def password(self, password: str) -> dict[str, Any]:
        if self.state != "password" or not password or len(password) > 256:
            raise AccountError("Сначала подключитесь по QR-коду.")
        try:
            await self.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise AccountError("Неверный облачный пароль Telegram.") from None
        self.state = "connected"
        return await self.status()

    async def groups(self, allowed: set[int]) -> list[dict[str, Any]]:
        await self.open()
        if self.state != "connected":
            raise AccountError("Подключите Telegram по QR-коду.")
        self.entities.clear()
        result = []
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group and dialog.id in allowed:
                self.entities[dialog.id] = dialog.entity
                result.append({"id": dialog.id, "title": dialog.name})
        return result

    async def history(self, chat: int, targets: set[int], start: datetime,
                      end: datetime, progress: Any) -> dict[int, list[dict[str, Any]]]:
        if chat not in self.entities:
            await self.groups({chat})
        entity = self.entities.get(chat)
        if entity is None or utils.get_peer_id(entity) != chat:
            raise AccountError("Эта группа недоступна подключённому аккаунту.")
        result: dict[int, list[dict[str, Any]]] = {target: [] for target in targets}
        scanned = 0
        async for message in self.client.iter_messages(entity, offset_date=end, limit=10001):
            if message.date < start:
                break
            scanned += 1
            if scanned > 10000:
                raise AccountError("В периоде больше 10 000 сообщений. Выберите меньший период.")
            progress(scanned)
            if (message.date >= end or message.sender_id not in targets
                    or not message.message or getattr(message, "action", None)):
                continue
            target = message.sender_id
            result[target].append({"id": message.id, "type": "message",
                                   "date_unixtime": str(int(message.date.timestamp())),
                                   "from_id": f"user{target}", "text": message.message})
        return result

    async def cancel_login(self) -> None:
        if self.login_task:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
            self.login_task = None

    async def disconnect(self) -> None:
        await self.cancel_login()
        if self.client is None and (self.directory / "account.session").exists():
            await self.open()
        if self.client:
            try:
                if not await self.client.log_out():
                    raise AccountError("Не удалось отозвать сессию. Попробуйте ещё раз.")
            finally:
                await self.client.disconnect()
                self.client = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            (self.directory / f"account.session{suffix}").unlink(missing_ok=True)
        (self.directory / "application.json").unlink(missing_ok=True)
        self.state, self.qr = "disconnected", None
        self.entities.clear()

    async def close(self) -> None:
        await self.cancel_login()
        if self.client:
            await self.client.disconnect()
            self.client = None
