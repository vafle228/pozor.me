"""Route normalized Telegram updates to legacy controls and conversation turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pozor.core.commands.parsing import CommandContext, ParsedCommand, _utf16_slice, parse_command
from pozor.core.commands.telegram_scope import topic_id
from pozor.core.usecases.legacy_round import may_continue_mention
from pozor.infra.config.registry import Settings
from pozor.infra.config.service import SettingsService
from pozor.infra.llm.queue import InferenceQueue
from pozor.infra.sqlite import round_events
from pozor.infra.sqlite.commands import close_session, expire_sessions, resolve_target
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.sqlite.ingestion import capture_reply_context


class EventPipeline(Protocol):
    database: Path
    bot: Any
    ready: bool
    queue: InferenceQueue
    generation_log: GenerationRepository
    control_handler: Callable[..., Awaitable[None]] | None
    settings: Settings
    settings_service: SettingsService | None

    def clock(self) -> float: ...
    def invalidate(self) -> None: ...
    def accept(
        self,
        session_id: str,
        message_id: int,
        text: str,
        *,
        first: bool = False,
        author_id: int | None = None,
    ) -> bool: ...
    async def notify(self, chat_id: int, thread_id: int, text: str) -> None: ...
    async def handle_message(
        self, payload: dict[str, object], bot_username: str | None
    ) -> None: ...


def _entities(raw: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    items = raw.get("entities")
    return tuple(e for e in items if isinstance(e, dict)) if isinstance(items, list) else ()


def _mention(text: str, entities: tuple[dict[str, Any], ...], username: str | None) -> str | None:
    if username is None:
        return None
    for entity in entities:
        offset, length = entity.get("offset"), entity.get("length")
        if (
            entity.get("type") == "mention"
            and type(offset) is int
            and type(length) is int
            and (_utf16_slice(text, offset, length) or "").casefold() == "@" + username.casefold()
        ):
            return username
    return None


def _same_round(database: Path, raw: dict[str, Any], command: ParsedCommand, bot: int) -> bool:
    text = raw["text"]
    chat, actor = raw.get("chat"), raw.get("from")
    if not (
        may_continue_mention(command, text) and isinstance(chat, dict) and isinstance(actor, dict)
    ):
        return False
    thread = topic_id(raw)
    chat_id, actor_id = chat.get("id"), actor.get("id")
    if type(chat_id) is not int or type(actor_id) is not int:
        return False
    current = round_events.participant_target(database, chat_id, thread, actor_id)
    if current is None:
        return False
    reply = raw.get("reply_to_message")
    reply_actor = reply.get("from") if isinstance(reply, dict) else None
    if isinstance(reply_actor, dict) and type(reply_actor.get("id")) is not int:
        return False
    target = reply_actor.get("id") if isinstance(reply_actor, dict) else None
    if target == bot:
        target = None
    if not command.target_count and target is None:
        return True
    context = CommandContext(
        chat_id,
        actor_id,
        int(raw["message_id"]),
        thread,
        text,
        _entities(raw),
        target,
    )
    return resolve_target(database, context, command, bot_user_ids=frozenset({bot})) == current


async def _rate(pipeline: EventPipeline, query: dict[str, Any]) -> None:
    try:
        _, generation, rating = query["data"].split(":")
        message, actor = query["message"], query["from"]
        if not isinstance(message, dict) or not isinstance(actor, dict) or actor.get("is_bot"):
            return
        chat = message["chat"]
        if not isinstance(chat, dict):
            return
        accepted = await asyncio.to_thread(
            pipeline.generation_log.rate,
            int(generation),
            chat=int(chat["id"]),
            thread=topic_id(message),
            user=int(actor["id"]),
            message_id=int(message["message_id"]),
            rating=int(rating),
        )
        await pipeline.bot.answer_callback_query(
            callback_query_id=query["id"],
            text="Оценка сохранена." if accepted else "Эта оценка недоступна.",
        )
    except (KeyError, ValueError, TypeError):
        return


async def handle_update(
    pipeline: EventPipeline,
    payload: dict[str, object],
    *,
    bot_id: int,
    bot_username: str | None,
    owner_ids: frozenset[int],
) -> None:
    query = payload.get("callback_query")
    if (
        isinstance(query, dict)
        and isinstance(query.get("data"), str)
        and query["data"].startswith("rate:")
    ):
        await _rate(pipeline, query)
        return
    raw = payload.get("message")
    if isinstance(raw, dict):
        stamp, text = raw.get("date"), raw.get("text")
        if (
            not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)
            or abs(pipeline.clock() - stamp) > 30
        ):
            return
        parsed = (
            parse_command(text, _entities(raw), bot_username=bot_username)
            if isinstance(text, str)
            else None
        )
        expire_sessions(pipeline.database, now=int(pipeline.clock()))
        if parsed and parsed.name == "roast":
            if _same_round(pipeline.database, raw, parsed, bot_id):
                await pipeline.handle_message(payload, bot_username)
                pipeline.invalidate()
                return
            queue = pipeline.queue
            if (
                not pipeline.ready
                or queue.closed
                or sum(map(len, queue.chats.values())) >= queue.capacity
            ):
                chat, actor = raw.get("chat"), raw.get("from")
                if (
                    isinstance(chat, dict)
                    and type(chat.get("id")) is int
                    and chat.get("type") in {"group", "supergroup"}
                    and isinstance(actor, dict)
                    and type(actor.get("id")) is int
                    and not actor.get("is_bot")
                    and not raw.get("sender_chat")
                ):
                    await pipeline.notify(
                        chat["id"],
                        topic_id(raw),
                        "Сейчас занят. Попробуйте позже."
                        if pipeline.ready
                        else "Генерация пока недоступна. Попробуйте позже.",
                    )
                return
    if pipeline.control_handler is None:
        raise RuntimeError("Telegram control handler was not composed")
    await pipeline.control_handler(
        pipeline.database,
        pipeline.bot,
        payload,
        owner_ids=owner_ids,
        bot_id=bot_id,
        bot_username=bot_username,
        settings=pipeline.settings_service.snapshot()
        if pipeline.settings_service
        else pipeline.settings,
    )
    await pipeline.handle_message(payload, bot_username)
    pipeline.invalidate()


async def handle_message(
    pipeline: EventPipeline, payload: dict[str, object], bot_username: str | None
) -> None:
    edit, raw = payload.get("edited_message"), payload.get("message")
    if isinstance(edit, dict):
        chat = edit.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        mid = edit.get("message_id")
        if type(chat_id) is int and type(mid) is int:
            round_events.edited_input(pipeline.database, chat_id, mid)
        pipeline.invalidate()
        return
    if not isinstance(raw, dict) or raw.get("sender_chat"):
        pipeline.invalidate()
        return
    chat, actor = raw.get("chat"), raw.get("from")
    text, mid, stamp = raw.get("text"), raw.get("message_id"), raw.get("date")
    if (
        not isinstance(chat, dict)
        or chat.get("type") not in {"group", "supergroup"}
        or type(chat.get("id")) is not int
        or not isinstance(actor, dict)
        or actor.get("is_bot")
        or type(actor.get("id")) is not int
        or not isinstance(text, str)
        or type(mid) is not int
        or not isinstance(stamp, (int, float))
        or abs(pipeline.clock() - stamp) > 30
    ):
        return
    chat_id, author, thread = chat["id"], actor["id"], topic_id(raw)
    reply = raw.get("reply_to_message")
    parent = reply.get("message_id") if isinstance(reply, dict) else None
    active = round_events.round_for_message(pipeline.database, chat_id, thread, author, parent)
    if active == "closed":
        await pipeline.notify(
            chat_id,
            thread,
            "Этот раунд завершён. Начни новый: /roast @username или /roast ответом на сообщение участника.",
        )
        return
    if active is None:
        return
    entities = _entities(raw)
    action = active.action(mid, author, text, _mention(text, entities, bot_username))
    if action == "ignore":
        return
    if action == "close":
        close_session(
            pipeline.database,
            session_id=active.session_id,
            chat_id=chat_id,
            thread_id=thread,
            actor_id=author,
        )
        pipeline.invalidate()
        return
    command = parse_command(text, entities, bot_username=bot_username)
    quote = capture_reply_context(pipeline.database, raw, active.target_id)
    first = action == "first"
    hint = active.hint(text, command, quote, first)
    pipeline.accept(active.session_id, mid, hint, first=first, author_id=author)
