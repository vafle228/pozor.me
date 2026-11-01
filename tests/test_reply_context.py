import time

import pytest

from pozor.ingestion import capture_reply_context
from pozor.privacy import forget
from pozor.storage import connect, migrate


@pytest.fixture
def scope(tmp_path):
    database = tmp_path / 'reply.sqlite3'
    migrate(database)
    with connect(database) as db:
        db.execute('INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1)')
        db.execute('INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1)')
    message = {'chat': {'id': -1}, 'message_thread_id': 3, 'is_topic_message': True, 'reply_to_message': {
        'chat': {'id': -1}, 'message_thread_id': 3, 'is_topic_message': True, 'message_id': 10,
        'from': {'id': 42}, 'date': int(time.time()), 'text': 'synthetic tournament quote'}}
    return database, message


@pytest.mark.parametrize('damage', ['chat', 'thread', 'author', 'bot', 'forward',
                                   'anonymous', 'optout', 'forget', 'disabled', 'command'])
def test_reply_capture_preserves_scope_privacy_and_attribution(scope, damage):
    database, message = scope
    reply = message['reply_to_message']
    if damage == 'chat':
        reply['chat']['id'] = -2
    elif damage == 'thread':
        reply['message_thread_id'] = 4
    elif damage == 'author':
        reply['from']['id'] = 43
    elif damage == 'bot':
        reply['from']['is_bot'] = True
    elif damage == 'forward':
        reply['forward_origin'] = {'type': 'user'}
    elif damage == 'anonymous':
        reply['sender_chat'] = {'id': -1}
    elif damage == 'forget':
        forget(database, -1, 42)
    elif damage == 'command':
        reply['text'] = '/roast_on'
        reply['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': 9}]
    else:
        with connect(database) as db:
            db.execute('UPDATE members SET opt_out=1' if damage == 'optout'
                       else 'UPDATE chats SET enabled=0')
    capture_reply_context(database, message, 42)
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


def test_reply_capture_does_not_overwrite_newer_observation_or_recurse(scope):
    database, message = scope
    message['reply_to_message']['reply_to_message'] = {'text': 'unrelated ancestor'}
    capture_reply_context(database, message, 42)
    message['reply_to_message']['text'] = 'stale embedded snapshot'
    capture_reply_context(database, message, 42)
    with connect(database) as db:
        rows = db.execute('SELECT text,reply_source_id FROM messages').fetchall()
        assert [tuple(row) for row in rows] == [('synthetic tournament quote', None)]
