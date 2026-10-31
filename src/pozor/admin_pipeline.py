"""One cancellable import job and expiring review drafts, owned by a dedicated loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import errors

from pozor import semantic_profiles
from pozor.config import Settings
from pozor.importing import apply_import, preview_import
from pozor.semantic_profiles import require_scope
from pozor.storage import connect
from pozor.telegram_account import AccountError, TelegramAccount, private_write

logger = logging.getLogger(__name__)


class AdminPipeline:
    def __init__(self, database: Path, settings: Settings):
        self.database = database.resolve()
        self.settings = settings
        self.account = TelegramAccount(settings, self.database.parent / "telegram")
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.lock: asyncio.Lock | None = None
        self.task: asyncio.Task[None] | None = None
        self.job: dict[str, Any] = {"state": "idle"}
        self.drafts: list[dict[str, Any]] = []

    def call(self, action: str, body: dict[str, Any] | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(self.dispatch(action, body or {}), self.loop)
        try:
            return future.result(timeout=25)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise AccountError("Telegram не ответил вовремя. Повторите попытку.") from None

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
            return {"groups": await self.account.groups(self.allowed())}
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
                    since=start, until=end)
                with connect(self.database) as db:
                    member = db.execute("SELECT display_name FROM members WHERE chat_id=? AND user_id=?",
                                        (chat, target)).fetchone()
                    draft["name"] = member[0] if member else None
                semantic_profiles.validate_draft(self.database, draft)
                self.drafts.append(draft)
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
        except Exception as exc:  # noqa: BLE001 - terminal job boundary; never expose exception text
            # Never strand a completed task in 'running', or log RPC/model payloads.
            logger.warning("Profile job failed (%s)", type(exc).__name__)
            self.drafts.clear()
            self.job.update(state="failed", message="Задача не завершена. Проверьте доступ к группе, "
                            "согласие и работу модели. Уже импортированные цитаты сохранены.")

    async def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.drafts.clear()
        self.job = {"state": "cancelled", "message": "Черновики очищены; импортированные цитаты сохранены."}

    def close(self) -> None:
        async def shutdown() -> None:
            await self.cancel()
            await self.account.close()
        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            if not self.thread.is_alive():
                self.loop.close()
