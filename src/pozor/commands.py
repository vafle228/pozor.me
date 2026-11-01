"""Compatibility API. Production uses typed presentation handlers."""
from copy import deepcopy
from pathlib import Path
from typing import Any

from pozor.infra.sqlite.commands import *


async def handle_telegram_update(database_path: Path, bot: Any, payload: dict[str,object], **kwargs: Any) -> None:
    from pozor.presentation.telegram_bot.commands import handle_telegram_update as handle
    # Old offline clients passed partial wire records. Fill only structural
    # defaults here; real polling enters the strict typed adapter directly.
    normalized = deepcopy(payload)
    def defaults(value: Any, parent_chat: Any = None) -> None:
        if isinstance(value,dict):
            if 'message_id' in value:
                value.setdefault('date',0)
                if parent_chat is not None:
                    value.setdefault('chat',deepcopy(parent_chat))
                if isinstance(value.get('chat'),dict):
                    value['chat'].setdefault('type','supergroup')
            if isinstance(value.get('from'),dict):
                value['from'].setdefault('is_bot',False)
                value['from'].setdefault('first_name','Участник')
            if isinstance(value.get('sender_chat'),dict):
                value['sender_chat'].setdefault('type','supergroup')
            if 'callback_query' in value and isinstance(value['callback_query'],dict):
                value['callback_query'].setdefault('chat_instance','compatibility')
            for child in list(value.values()):
                defaults(child,value.get('chat',parent_chat))
        elif isinstance(value,list):
            for child in value:
                defaults(child,parent_chat)
    defaults(normalized)
    normalized.setdefault('update_id',0)
    await handle(database_path,bot,normalized,**kwargs)
