from types import SimpleNamespace
from unittest.mock import AsyncMock

from pozor.config import Settings
from pozor.infra.sqlite.connection import connect, migrate
from pozor.presentation.telegram_bot.commands import TelegramControls


async def test_durable_inbox_record_reaches_real_aiogram_router(tmp_path):
    database=tmp_path/'router.sqlite3'
    migrate(database)
    with connect(database) as db:
        db.execute('INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1)')
    bot=SimpleNamespace(id=99,send_message=AsyncMock(),get_chat_member=AsyncMock())
    settings=Settings(_env_file=None,database_path=database)
    controls=TelegramControls(database,owner_ids=frozenset(),bot_id=99,bot_username='pozor_bot',settings=settings)
    try:
        await controls(database,bot,{'update_id':1,'message':{'message_id':1,'date':1793500000,
            'chat':{'id':-1,'type':'supergroup'},'from':{'id':42,'is_bot':False,'first_name':'Synthetic'},
            'text':'/roast_on 1','entities':[{'type':'bot_command','offset':0,'length':9}]}},
            owner_ids=frozenset(),bot_id=99,settings=settings)
        with connect(database) as db:
            row=db.execute('SELECT consent,roast_level FROM members WHERE chat_id=-1 AND user_id=42').fetchone()
            assert tuple(row)==(1,1)
        bot.send_message.assert_awaited_once()
    finally:
        await controls.aclose()
