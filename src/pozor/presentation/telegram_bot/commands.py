"""Typed Telegram control handlers. Database work runs outside the event loop."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Literal

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from pydantic import ValidationError

from pozor.config import Settings
from pozor.core.commands.parsing import CommandContext, CommandError, parse_command, roast_hint
from pozor.core.commands.telegram_scope import topic_id
from pozor.core.humor.levels import requested_level
from pozor.infra.sqlite import command_store as store
from pozor.infra.sqlite.commands import (
    callback_action,
    callback_data,
    callback_session_id,
    close_session,
    resolve_target,
    set_chat_enabled,
    set_consent,
    start_session,
    whois,
)
from pozor.infra.sqlite.ingestion import capture_reply_context
from pozor.infra.sqlite.privacy_repo import forget
from pozor.infra.sqlite.retrieval import retrieve


async def handle_message(database: Path, bot: Bot, message: Message, *, owner_ids: frozenset[int],
                         bot_id: int, bot_username: str | None, settings: Settings | None) -> None:
    actor = message.from_user
    if message.chat.type not in {'group','supergroup'} or actor is None or actor.is_bot or not message.text:
        return
    entities = tuple(e.model_dump(mode='python',by_alias=True,exclude_none=True) for e in message.entities or [])
    parsed = parse_command(message.text,entities,bot_username=bot_username)
    if parsed is None:
        return
    chat, user, mid = message.chat.id,actor.id,message.message_id
    raw = message.model_dump(mode='json',by_alias=True,exclude_none=True,exclude_unset=True)
    thread = topic_id(raw)
    if message.sender_chat and parsed.name in {'forget_me','roast_on','roast_off'}:
        return
    if await asyncio.to_thread(store.is_blocked,database,chat,user) and parsed.name not in {'forget_me','roast_on'}:
        return
    reply_actor = message.reply_to_message.from_user if message.reply_to_message else None
    context = CommandContext(chat,user,mid,thread,message.text,entities,
        reply_actor.id if reply_actor else None,reply_actor.is_bot if reply_actor else False,
        False,message.sender_chat is not None)
    await asyncio.to_thread(store.observe,database,chat,user,actor.full_name,actor.username)
    response = ''
    markup = None

    async def admin() -> bool:
        if user in owner_ids:
            return True
        if message.sender_chat:
            return False
        try:
            member = await bot.get_chat_member(chat_id=chat,user_id=user)
            return member.status in {ChatMemberStatus.CREATOR,ChatMemberStatus.ADMINISTRATOR}
        except TelegramAPIError:
            return False

    if parsed.name in {'enable_chat','disable_chat'}:
        if not await admin():
            response = 'Команду может выполнить только владелец или администратор этой группы.'
        elif await asyncio.to_thread(set_chat_enabled,database,chat,parsed.name=='enable_chat'):
            response = 'Чат включён.' if parsed.name=='enable_chat' else 'Чат выключен; активные сессии завершены.'
        else:
            response = 'Чат не подключён. Включите его через админку в разделе «Чаты» или «История Telegram».'
    elif parsed.name=='help':
        response = ('/roast меня или /roast @username; /roast_on 1|2|3 — согласие и максимум жёсткости; '
                    '/roast_off — выключить; /stop, /finish — завершить; /whois — ответом; /forget_me — удалить данные. '
                    '/enable_chat, /disable_chat — управление группой. ')
        if bot_username:
            response += f'Для диалога: @{bot_username} как дела? Для прожарки: @{bot_username} прожарь меня. '
            response += 'Бот должен видеть сообщения группы: privacy mode выключен или бот — администратор.'
    elif parsed.name=='whois':
        found = await asyncio.to_thread(whois,database,chat,reply_actor.id) if reply_actor and not reply_actor.is_bot else None
        response = found or 'Используйте /whois ответом на сообщение участника.'
    elif parsed.name=='forget_me':
        if not await asyncio.to_thread(store.allowed,database,chat,enabled=False):
            return
        await asyncio.to_thread(forget,database,chat,user)
        response = 'Данные в этой группе удалены, участие выключено. /roast_on разрешит новый сбор и импорт.'
    elif parsed.name in {'roast_on','roast_off'}:
        if parsed.name=='roast_on' and parsed.argument not in {'','1','2','3'}:
            response = 'Используйте /roast_on 1, 2 или 3.'
        elif await asyncio.to_thread(set_consent,database,chat,user,parsed.name=='roast_on'):
            if parsed.name=='roast_on':
                await asyncio.to_thread(store.consent_level,database,chat,user,int(parsed.argument or '2'))
            response = 'Участие включено для этого чата.' if parsed.name=='roast_on' else 'Участие выключено; активные сессии завершены.'
        else:
            response = 'Сначала оператор должен включить этот чат.'
    elif parsed.name=='roast':
        target = await asyncio.to_thread(resolve_target,database,context,parsed,bot_user_ids=frozenset({bot_id}))
        if parsed.target_count>1:
            response = 'Укажите одного участника.'
        elif target is None:
            response = 'Укажите одного известного участника username или ответом на его сообщение.'
        else:
            try:
                if not await asyncio.to_thread(store.allowed,database,chat,enabled=True):
                    raise CommandError('Чат не включён оператором')
                mode: Literal['roast','dialogue'] = 'dialogue' if (parsed.conversational and parsed.target_count==0 and
                    (reply_actor is None or reply_actor.is_bot) and not re.match(r'^(?:меня|себя|me)(?=\s|$)',parsed.argument,re.IGNORECASE)) else 'roast'
                await asyncio.to_thread(capture_reply_context,database,raw,target)
                hint = roast_hint(parsed)
                pack = await asyncio.to_thread(retrieve,database,chat_id=chat,thread_id=thread,target_id=target,
                    trigger_id=mid,hint=hint,target_label='участник')
                sid = await asyncio.to_thread(start_session,database,chat_id=chat,thread_id=thread,target_id=target,
                    initiator_id=user,trigger_message_id=mid,mode=mode,
                    material_available=mode=='dialogue' or bool(hint or pack.recent or pack.evidence),
                    level=min(settings.roast_level if settings else 2,requested_level(parsed.argument)),
                    daily_limit=settings.target_daily_round_limit if settings else 12)
                label = await asyncio.to_thread(store.label,database,chat,target)
                response = '' if mode=='dialogue' else f'Цель прожарки: {label}. Раунд — до 4 ответов. Продолжай тегом бота или ответом на панч; цель останется той же. /stop — завершить.'
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='Стоп',callback_data=callback_data('stop',sid)),
                    InlineKeyboardButton(text='Завершить',callback_data=callback_data('finish',sid))]])
            except CommandError as exc:
                response = str(exc)
    elif parsed.name in {'stop','finish'}:
        closed = await asyncio.to_thread(close_session,database,session_id=None,chat_id=chat,thread_id=thread,
                                         actor_id=user,is_admin=user in owner_ids)
        if not closed and await admin():
            closed = await asyncio.to_thread(close_session,database,session_id=None,chat_id=chat,thread_id=thread,
                                             actor_id=user,is_admin=True)
        response = 'Сессия завершена.' if closed else 'Нет доступной вам активной сессии в этой теме.'
    else:
        return
    if await asyncio.to_thread(store.receipt,database,chat,mid,response) and response:
        await bot.send_message(chat_id=chat,text=response,message_thread_id=thread or None,reply_markup=markup,parse_mode=None)


async def handle_callback(database: Path, bot: Bot, query: CallbackQuery) -> None:
    if not isinstance(query.message,Message):
        return
    if query.data and query.data.startswith('eval:') and not query.from_user.is_bot:
        from pozor.infra.sqlite.humor_eval_repo import vote
        try:
            _,pair,choice=query.data.split(':')
            accepted=await asyncio.to_thread(vote,database,int(pair),query.message.chat.id,
                                             query.message.message_id,query.from_user.id,choice)
            await bot.answer_callback_query(callback_query_id=query.id,
                text='Оценка сохранена.' if accepted else 'Эта пара недоступна.')
        except (ValueError,TypeError):
            return
        return
    action,sid = callback_action(query.data),callback_session_id(query.data)
    if action is None or sid is None:
        return
    raw = query.message.model_dump(mode='json',by_alias=True,exclude_none=True,exclude_unset=True)
    closed = await asyncio.to_thread(close_session,database,session_id=sid,chat_id=query.message.chat.id,
                                     thread_id=topic_id(raw),actor_id=query.from_user.id)
    await bot.answer_callback_query(callback_query_id=query.id,
        text='Сессия завершена.' if closed else 'Эта кнопка недоступна для вашего аккаунта.')


async def handle_telegram_update(database_path: Path, bot: Bot, payload: dict[str, object], *,
    owner_ids: frozenset[int], bot_id: int, bot_username: str | None = None, settings: Settings | None = None) -> None:
    try:
        update = Update.model_validate(payload)
    except ValidationError:
        return
    if update.callback_query:
        await handle_callback(database_path,bot,update.callback_query)
    elif update.message:
        await handle_message(database_path,bot,update.message,owner_ids=owner_ids,
            bot_id=bot_id,bot_username=bot_username,settings=settings)


def router(database: Path, *, owner_ids: frozenset[int], bot_id: int, bot_username: str | None,
           settings: Settings) -> Router:
    result = Router(name='pozor-controls')
    async def message_handler(message: Message,bot: Bot,runtime_settings: Settings | None = None) -> None:
        await handle_message(database,bot,message,owner_ids=owner_ids,bot_id=bot_id,bot_username=bot_username,settings=runtime_settings or settings)
    async def callback_handler(query: CallbackQuery,bot: Bot) -> None:
        await handle_callback(database,bot,query)
    result.message.register(message_handler)
    result.callback_query.register(callback_handler)
    return result


class TelegramControls:
    """Feed durable inbox records through aiogram without a second poller."""

    def __init__(self,database: Path,*,owner_ids: frozenset[int],bot_id: int,
                 bot_username: str | None,settings: Settings):
        self.dispatcher=Dispatcher(disable_fsm=True)
        self.dispatcher.include_router(router(database,owner_ids=owner_ids,bot_id=bot_id,
            bot_username=bot_username,settings=settings))

    async def __call__(self,database_path: Path,bot: Bot,payload: dict[str,object],*,
                       owner_ids: frozenset[int],bot_id: int,bot_username: str | None = None,
                       settings: Settings | None = None) -> None:
        try:
            update=Update.model_validate(payload)
        except ValidationError:
            return
        await self.dispatcher.feed_update(bot,update,runtime_settings=settings)

    async def aclose(self) -> None:
        await self.dispatcher.storage.close()
