"""Optional user-account reader. All methods run on the admin's dedicated event loop."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import qrcode
import qrcode.image.svg
from telethon import TelegramClient, errors, types, utils
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest

from pozor.config import Settings
from pozor.infra.files.private import private_write, protect

__all__=["AccountError", "TelegramAccount", "private_write"]


class AccountError(ValueError):
    """Safe, user-facing error, without RPC details or account credentials."""


class TelegramAccount:
    def __init__(self, settings: Settings, directory: Path):
        self.settings = settings
        self.directory = directory
        self.client: Any = None
        self.login_task: asyncio.Task[None] | None = None
        self.state = "disconnected"
        self.qr: str | None = None
        self.entities: dict[int, Any] = {}
        self.credentials_source = "env"

    def credentials(self) -> tuple[int | None, str | None]:
        path = self.directory.parent / "secrets.local.json"
        legacy = self.directory / "application.json"
        for source in (path, legacy):
            if source.exists():
                try:
                    saved = json.loads(source.read_text("utf-8"))
                    api_id, api_hash = saved["api_id"], saved["api_hash"]
                    if type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str) or len(api_hash) != 32:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise AccountError("Повреждён файл реквизитов Telegram. Сохраните их заново.") from None
                if source == legacy:
                    private_write(path, json.dumps(saved))
                    legacy.unlink()
                self.credentials_source = "admin"
                return api_id, api_hash
        self.credentials_source = "env"
        return (self.settings.telegram_api_id,
                self.settings.telegram_api_hash.get_secret_value() if self.settings.telegram_api_hash else None)

    async def open(self) -> None:
        if self.client is not None:
            return
        api_id, api_hash = self.credentials()
        if not (api_id and api_hash):
            raise AccountError("Укажите API ID и API Hash приложения Telegram.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        await asyncio.to_thread(protect,self.directory,directory=True)
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
        api_id, api_hash = self.credentials()
        return {"state": self.state, "qr": self.qr,
                "configured": bool(api_id and api_hash), "credentials_source": self.credentials_source}

    async def start(self, api_id: int | None = None, api_hash: str | None = None) -> dict[str, Any]:
        if self.state == "connected" and api_id is None and not api_hash:
            return await self.status()
        if api_id is not None or api_hash:
            if (type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str)
                    or len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash)):
                raise AccountError("Проверьте API ID и API Hash.")
            await self.close()
            private_write(self.directory.parent / "secrets.local.json",
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

    async def groups(self, allowed: set[int] | None = None) -> list[dict[str, Any]]:
        await self.open()
        if self.state != "connected":
            raise AccountError("Подключите Telegram по QR-коду.")
        self.entities.clear()
        result = []
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group and (allowed is None or dialog.id in allowed):
                self.entities[dialog.id] = dialog.entity
                result.append({"id": dialog.id, "title": dialog.name})
        return result

    async def add_bot(self, chat: int, username: str) -> str:
        groups = await self.groups()
        group = next((g for g in groups if g['id'] == chat), None)
        if group is None:
            raise AccountError("Эта группа недоступна подключённому аккаунту.")
        entity = self.entities[chat]
        try:
            if isinstance(entity, types.Chat):
                await self.client(AddChatUserRequest(entity.id, username, fwd_limit=0))
            else:
                await self.client(InviteToChannelRequest(entity, [username]))
        except errors.UserAlreadyParticipantError:
            pass
        return cast(str, group['title'])

    async def history(self, chat: int, targets: set[int], start: datetime,
                      end: datetime, progress: Any) -> dict[int, list[dict[str, Any]]]:
        if chat not in self.entities:
            await self.groups({chat})
        entity = self.entities.get(chat)
        if entity is None or utils.get_peer_id(entity) != chat:
            raise AccountError("Эта группа недоступна подключённому аккаунту.")
        result: dict[int, list[dict[str, Any]]] = {target: [] for target in targets}
        scanned = 0
        def serialize(message: Any) -> dict[str, Any] | None:
            if (not message or not message.sender_id or not message.message or getattr(message,'action',None)
                    or getattr(getattr(message,'sender',None),'bot',False)
                    or not start <= message.date < end):
                return None
            row = {"id":message.id,"type":"message","date_unixtime":str(int(message.date.timestamp())),
                   "from_id":f"user{message.sender_id}","text":message.message}
            if parent := getattr(message,'reply_to_msg_id',None):
                row['reply_to_message_id'] = parent
            if thread := getattr(getattr(message,'reply_to',None),'reply_to_top_id',None):
                row['message_thread_id'] = thread
            if getattr(message,'fwd_from',None):
                row['forwarded_from'] = 'forwarded'
            return row
        for target in sorted(targets):
            collected: dict[int,dict[str,Any]] = {}
            count = 0
            async for message in self.client.iter_messages(entity,from_user=target,offset_date=end,limit=10001):
                if message.date < start:
                    break
                if message.sender_id != target or message.date >= end:
                    continue
                count += 1
                scanned += 1
                if count > 10000:
                    raise AccountError('У участника больше 10 000 сообщений. Выберите меньший период.')
                progress(scanned)
                row = serialize(message)
                if row is None:
                    continue
                collected[row['id']] = row
                ids = [message.id-1,message.id+1]
                if parent := getattr(message,'reply_to_msg_id',None):
                    ids.append(parent)
                if hasattr(self.client,'get_messages'):
                    # Request only a bounded neighbourhood, in the same verified peer.
                    nearby = await self.client.get_messages(entity,ids=list(dict.fromkeys(i for i in ids if i>0)))
                    for other in nearby:
                        related = serialize(other)
                        if related and (related['id']==row.get('reply_to_message_id')
                            or abs((other.date-message.date).total_seconds())<=300):
                            collected.setdefault(related['id'],related)
            result[target] = sorted(collected.values(),key=lambda r:(r['date_unixtime'],r['id']))
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
        (self.directory.parent / "secrets.local.json").unlink(missing_ok=True)
        self.state, self.qr = "disconnected", None
        self.entities.clear()

    async def close(self) -> None:
        await self.cancel_login()
        if self.client:
            await self.client.disconnect()
            self.client = None
