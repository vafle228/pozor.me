"""One cancellable import job and expiring review drafts, owned by a dedicated loop."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from telethon import errors

from pozor import semantic_profiles
from pozor.config import Settings
from pozor.importing import apply_import, preview_import
from pozor.inference_queue import InferenceQueue
from pozor.llm import InferenceError
from pozor.semantic_profiles import require_scope
from pozor.storage import connect
from pozor.telegram_account import AccountError, TelegramAccount, private_write

logger = logging.getLogger(__name__)


class AdminPipeline:
    def __init__(self, database: Path, settings: Settings, queue: InferenceQueue | None = None):
        self.database = database.resolve()
        self.settings = settings
        self.account = TelegramAccount(settings, self.database.parent / "telegram")
        self.queue = queue
        self.lock: asyncio.Lock | None = None
        self.task: asyncio.Task[None] | None = None
        self.job: dict[str, Any] = {"state": "idle"}
        self.drafts: list[dict[str, Any]] = []

    async def dispatch(self, action: str, body: dict[str, Any]) -> Any:
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            try:
                return await self._dispatch(action, body)
            except errors.FloodWaitError as exc:
                raise AccountError(f"Telegram просит подождать {exc.seconds} секунд.") from None
            except (errors.RPCError, OSError):
                raise AccountError("Не удалось связаться с Telegram. Повторите подключение.") from None
            except TelegramAPIError:
                raise AccountError("Не удалось добавить бота. Проверьте право приглашать участников "
                                   "и доступ бота к группе.") from None

    def allowed(self) -> set[int]:
        with connect(self.database) as db:
            return {r[0] for r in db.execute("SELECT chat_id FROM chats WHERE allowed=1 AND enabled=1")}

    def members(self, chat: int) -> list[dict[str, Any]]:
        with connect(self.database) as db:
            if chat not in self.allowed():
                raise AccountError("Группа не разрешена или сбор выключен.")
            return [dict(row) for row in db.execute("""SELECT user_id,display_name,current_username
                FROM members m WHERE chat_id=? AND consent=1 AND opt_out=0 AND NOT EXISTS
                (SELECT 1 FROM forgotten_members f WHERE f.chat_id=m.chat_id AND f.user_id=m.user_id)
                ORDER BY coalesce(display_name,''),user_id""", (chat,))]

    async def _dispatch(self, action: str, body: dict[str, Any]) -> Any:
        if action == "status":
            return await self.account.status()
        if action == "connect":
            if self.task and not self.task.done():
                raise AccountError("Сначала дождитесь или отмените загрузку истории.")
            return await self.account.start(body.get("api_id"), body.get("api_hash"))
        if action == "password":
            return await self.account.password(body.get("password", ""))
        if action == "disconnect":
            await self.cancel()
            await self.account.disconnect()
            return {"state": "disconnected"}
        if action == "groups":
            with connect(self.database) as db:
                chats = {r["chat_id"]: bool(r["allowed"] and r["enabled"])
                         for r in db.execute("SELECT chat_id,allowed,enabled FROM chats")}
            groups = await self.account.groups(set(chats))
            return {"groups": [group | {"enabled": chats[group["id"]]}
                               for group in groups if group["id"] in chats]}
        if action == "available_groups":
            return {"groups": await self.account.groups()}
        if action == "add_chat":
            chat = body.get("chat")
            if type(chat) is not int or chat >= 0:
                raise AccountError("Выберите группу подключённого аккаунта.")
            if self.task and not self.task.done():
                raise AccountError("Сначала дождитесь или отмените загрузку истории.")
            if self.settings.bot_token is None:
                raise AccountError("Для добавления бота настройте BOT_TOKEN.")
            groups = await self.account.groups()
            group = next((group for group in groups if group["id"] == chat), None)
            if group is None:
                raise AccountError("Эта группа недоступна подключённому аккаунту.")
            title = group["title"]
            async with Bot(token=self.settings.bot_token.get_secret_value()) as bot:
                identity = await bot.get_me()
                try:
                    member = await bot.get_chat_member(chat, identity.id)
                except TelegramBadRequest:
                    member = None
                if member is None or member.status not in {"member", "administrator", "creator"}:
                    if identity.username is None:
                        raise AccountError("У Telegram-бота отсутствует username.")
                    title = await self.account.add_bot(chat, identity.username)
                    member = await bot.get_chat_member(chat, identity.id)
                if member.status not in {"member", "administrator", "creator"}:
                    raise AccountError("Telegram не подтвердил добавление бота в группу.")
            with connect(self.database) as db:
                db.execute("""INSERT INTO chats(chat_id,allowed,enabled,managed_by_admin,title,
                    retention_days) VALUES (?,1,1,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                    allowed=1,enabled=1,managed_by_admin=1,title=excluded.title""",
                           (chat, title, self.settings.live_retention_days))
            return {"chat": chat, "title": title}
        if action == "members":
            return {"members": self.members(int(body["chat"]))}
        if action == "job":
            if self.drafts:
                try:
                    for current_draft in self.drafts:
                        semantic_profiles.validate_draft(self.database, current_draft)
                except AccountError:
                    self.drafts.clear()
                    self.job = {"state": "expired", "message": "Источники изменились или черновик устарел."}
            return self.job | {"drafts": self.drafts}
        if action == "cancel":
            await self.cancel()
            return self.job
        if action == "start":
            if self.task and not self.task.done():
                raise AccountError("Загрузка уже идёт.")
            chat = body["chat"]
            targets = body["targets"]
            if (type(chat) is not int or not isinstance(targets, list) or not 1 <= len(targets) <= 10
                    or any(type(t) is not int for t in targets) or len(set(targets)) != len(targets)):
                raise AccountError("Выберите от 1 до 10 участников.")
            start = datetime.fromisoformat(body["from"]).replace(tzinfo=UTC)
            end = datetime.fromisoformat(body["until"]).replace(tzinfo=UTC)
            if start >= end or (end - start).days > 366:
                raise AccountError("Выберите период до года; конечная дата не включается (UTC).")
            for target in targets:
                with connect(self.database) as db:
                    require_scope(db, chat, target)
            await self.account.open()
            if self.account.state != "connected":
                raise AccountError("Сначала подключите Telegram.")
            self.drafts.clear()
            self.job = {"state": "running", "stage": "history", "scanned": 0,
                        "chat": chat, "targets": targets, "imported": 0}
            self.task = asyncio.create_task(self.run(chat, targets, start, end))
            return self.job
        if action == "save":
            draft: dict[str, Any] | None = next(
                (d for d in self.drafts if d["id"] == body.get("draft")), None)
            if draft is None:
                raise AccountError("Черновик не найден. Составьте профиль ещё раз.")
            selections = body.get("selections")
            if not isinstance(selections, list) or any(not isinstance(s, dict) for s in selections):
                raise AccountError("Выберите наблюдения для сохранения.")
            count = semantic_profiles.save(self.database, draft, selections)
            self.drafts.remove(draft)
            return {"saved": count}
        raise AccountError("Неизвестное действие.")

    async def run(self, chat: int, targets: list[int], start: datetime, end: datetime) -> None:
        try:
            rows = await self.account.history(chat, set(targets), start, end,
                                              lambda n: self.job.update(scanned=n))
            directory = self.database.parent / "staging"
            for target in targets:
                with connect(self.database) as db:
                    require_scope(db, chat, target)
                messages = rows[target]
                self.job.update(stage="import", target=target)
                if messages:
                    # Only self messages are exported; there are no neighbour/third-party records.
                    # Peer identity is checked against MTProto's marked ID before this conversion.
                    source_id = self.account.entities[chat].id
                    # Keep the namespace identical to Desktop exports of this exact group.
                    export = {"id": source_id, "type": "private_supergroup", "messages": messages}
                    file = directory / f"account-{chat}-{target}.json"
                    manifest = directory / f"account-{chat}-{target}-manifest.json"
                    private_write(file, json.dumps(export, ensure_ascii=False))
                    try:
                        preview_import(self.database, file_path=file, format="telegram-desktop",
                                       chat_id=chat, target_id=target, manifest_path=manifest,
                                       source_chat_id=source_id, from_date=start.date().isoformat(),
                                       until_date=end.date().isoformat(), timezone="UTC")
                        # Recheck consent immediately before activation, not only before download.
                        with connect(self.database) as db:
                            require_scope(db, chat, target)
                        _, count = apply_import(self.database, manifest, require_consent=True)
                        self.job["imported"] += count
                    finally:
                        file.unlink(missing_ok=True)
                        manifest.unlink(missing_ok=True)
                self.job.update(stage="analysis", target=target)
                draft = await semantic_profiles.analyze(
                    self.database, self.settings, chat, target,
                    lambda done, total: self.job.update(analyzed=done, sampled=total),
                    since=start, until=end, queue=self.queue)
                with connect(self.database) as db:
                    member = db.execute("SELECT display_name FROM members WHERE chat_id=? AND user_id=?",
                                        (chat, target)).fetchone()
                    draft["name"] = member[0] if member else None
                semantic_profiles.validate_draft(self.database, draft)
                self.drafts.append(draft)
                if self.queue is not None:
                    from pozor.infra.sqlite.hook_repo import refresh_hooks
                    await refresh_hooks(self.database,self.settings,self.queue,chat,target)
            self.job.update(state="review", stage="done")
        except asyncio.CancelledError:
            self.drafts.clear()
            self.job.update(state="cancelled", message="Задача отменена. Уже импортированные цитаты сохранены.")
            raise
        except errors.FloodWaitError as exc:
            self.job.update(state="failed", message=f"Telegram просит подождать {exc.seconds} секунд.")
        except AccountError as exc:
            self.drafts.clear()
            self.job.update(state="failed", message=str(exc) + " Уже импортированные цитаты сохранены.")
        except InferenceError:
            self.drafts.clear()
            logger.warning("Profile job failed at analysis (InferenceError)")
            self.job.update(state="failed", message="Цитаты сохранены, но модель не смогла составить профиль. "
                            "Повторите составление профиля; повторный импорт не создаст дубликаты.")
        except Exception as exc:  # noqa: BLE001 - terminal job boundary; never expose exception text
            # Never strand a completed task in 'running', or log RPC/model payloads.
            logger.warning("Profile job failed at %s (%s)", self.job.get("stage"), type(exc).__name__)
            self.drafts.clear()
            self.job.update(state="failed", message="Задача не завершена. Проверьте доступ к группе, "
                            "согласие и работу модели. Уже импортированные цитаты сохранены.")

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.drafts.clear()
        self.job = {"state": "cancelled", "message": "Черновики очищены; импортированные цитаты сохранены."}

    async def aclose(self) -> None:
        await self.cancel()
        await self.account.close()
