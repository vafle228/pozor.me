"""Aiogram transport for an already claimed legacy turn."""

import asyncio
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from pozor.core.usecases.deliver_turn import OutgoingTurn, SendResult
from pozor.infra.sqlite.commands import callback_data


class TelegramTurnTransport:
    def __init__(self, bot: Any):
        self.bot = bot

    async def send(self, turn: OutgoingTurn, timeout: float) -> SendResult:
        buttons = [
            [
                InlineKeyboardButton(
                    text="Стоп", callback_data=callback_data("stop", turn.session_id)
                ),
                InlineKeyboardButton(
                    text=f"Завершить · {turn.turn_no}/4",
                    callback_data=callback_data("finish", turn.session_id),
                ),
            ]
        ]
        if turn.rating_id is not None:
            buttons.append(
                [
                    InlineKeyboardButton(text=label, callback_data=f"rate:{turn.rating_id}:{value}")
                    for label, value in [("🔥", 1), ("😐", 0), ("💩", -1)]
                ]
            )
        try:
            async with asyncio.timeout(timeout):
                sent = await self.bot.send_message(
                    chat_id=turn.chat_id,
                    message_thread_id=turn.thread_id or None,
                    reply_parameters=ReplyParameters(
                        message_id=turn.reply_to, allow_sending_without_reply=False
                    ),
                    text=turn.text,
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                )
        except TelegramRetryAfter as exc:
            return SendResult("ready", "429", retry_after=exc.retry_after)
        except TelegramForbiddenError:
            return SendResult("cancelled", "403")
        except (TelegramAPIError, OSError, TimeoutError):
            return SendResult("unknown", "ambiguous_http")
        return SendResult("sent", message_id=sent.message_id)
