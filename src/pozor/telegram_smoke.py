"""Explicit live Bot API probe; never return identities, messages or credentials."""

from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from pozor.config import Settings
from pozor.storage import connect


class TelegramSmokeError(RuntimeError):
    pass


async def probe(bot: Any, settings: Settings, *, send: bool = False) -> dict[str, object]:
    identity = await bot.get_me()
    webhook = await bot.get_webhook_info()
    if webhook.url:
        raise TelegramSmokeError("webhook_active")
    checked = 0
    history_access = 0
    with connect(settings.database_path) as db:
        chat_ids = [row[0] for row in db.execute(
            "SELECT chat_id FROM chats WHERE allowed=1 AND enabled=1 ORDER BY chat_id DESC")]
    for chat_id in chat_ids:
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramAPIError:
            raise TelegramSmokeError("configured_chat_inaccessible") from None
        if chat.type not in ("group", "supergroup"):
            raise TelegramSmokeError("registered_chat_requires_group")
        member = await bot.get_chat_member(chat_id, identity.id)
        if member.status in ("left", "kicked"):
            raise TelegramSmokeError("bot_not_member")
        if getattr(identity, 'can_read_all_group_messages', False) or member.status == 'administrator':
            history_access += 1
        checked += 1
    if send:
        if not chat_ids:
            raise TelegramSmokeError("no_enabled_test_chat")
        message = await bot.send_message(
            chat_ids[0],
            "Техническая проверка pozor.me: Telegram Bot API подключён. Тестовое сообщение.",
            disable_notification=True,
        )
        await bot.delete_message(message.chat.id, message.message_id)
    return {"authentication": "ok", "webhook": "absent", "chats_checked": checked,
            "chats_with_full_message_access": history_access,
            "send_delete": "ok" if send else "not_requested"}


async def smoke(settings: Settings, *, send: bool = False) -> dict[str, object]:
    if not settings.bot_token:
        raise TelegramSmokeError("missing_token")
    async with Bot(token=settings.bot_token.get_secret_value()) as bot:
        return await probe(bot, settings, send=send)
