"""Automatic portraits: scheduling, privacy invalidation and interactive preemption."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from pozor import semantic_profiles
from pozor.admin import AdminData
from pozor.auto_profiles import refresh
from pozor.config import Settings
from pozor.inference_queue import InferenceQueue
from pozor.llm import InferenceError, LlamaClient
from pozor.privacy import forget
from pozor.storage import connect, migrate


def add(database, first, count):
    with connect(database) as db:
        for mid in range(first, first + count):
            text = f'Играю в шахматы, партия {mid}'
            db.execute('''INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,
                author_id,text,text_hash,occurred_at,origin) VALUES (?,-1,'live:-1',?,42,?,?,?,'live')''',
                       (mid, mid, text, hashlib.sha256(text.encode()).hexdigest(), datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO message_subjects VALUES (?,42,'self','observed_self')", (mid,))


@pytest.fixture
def database(tmp_path):
    database = tmp_path / 'portraits.sqlite3'
    migrate(database)
    with connect(database) as db:
        db.execute('INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1)')
        db.execute('INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1)')
    add(database, 1, 10)
    return database


@pytest.fixture
def model(monkeypatch):
    calls = []

    def respond(request):
        if request.url.path == '/apply-template':
            return httpx.Response(200, json={'prompt': 'rendered'})
        if request.url.path == '/tokenize':
            return httpx.Response(200, json={'tokens': [1] * 100})
        rows = json.loads(json.loads(request.content)['messages'][1]['content'])
        if json.loads(request.content)['response_format']['schema'].get('title') == 'Dossier':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps({'summary': 'Играет в шахматы и рассказывает о партиях.',
                    'source_ids': [rows[0]['sources'][0]['id']]})}}]})
        calls.append(rows)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'observations': [{'topic': 'Шахматы', 'kind': 'interest',
                'summary': 'Рассказывает об игре в шахматы', 'source_ids': [rows[0]['id']]}]})}}]})

    monkeypatch.setattr(semantic_profiles, 'LlamaClient', lambda settings: LlamaClient(
        settings, transport=httpx.MockTransport(respond)))
    return calls


async def idle(deadline):
    pass


def release(database):
    with connect(database) as db:
        db.execute('UPDATE automatic_profiles SET attempted_at=0')


@pytest.mark.asyncio
async def test_build_existing_messages_then_refresh_threshold(database, model):
    queue = InferenceQueue(idle, background_timeout=60)
    settings = Settings()
    try:
        assert await refresh(database, settings, queue, -1, 42)
        data = AdminData(database).read('/api/profiles', {'chat': ['-1']})['profiles'][0]
        assert {card['profile_version'] for card in data['cards']} == {3, 4}
        assert data['automatic']['state'] == 'ready'
        assert data['automatic']['analyzed'] == 10
        release(database)
        assert not await refresh(database, settings, queue, -1, 42)
        add(database, 11, 1)
        assert not await refresh(database, settings, queue, -1, 42)
        with connect(database) as db:
            db.execute('UPDATE automatic_profiles SET updated_at=?', (time.time() - 601,))
        assert await refresh(database, settings, queue, -1, 42)
        release(database)
        add(database, 12, 20)
        assert await refresh(database, settings, queue, -1, 42)
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_insufficient_messages_do_not_use_model(database, model):
    with connect(database) as db:
        db.execute('DELETE FROM messages WHERE id>9')
    queue = InferenceQueue(idle)
    assert not await refresh(database, Settings(), queue, -1, 42)
    assert model == []
    with connect(database) as db:
        assert db.execute('SELECT state FROM automatic_profiles').fetchone()[0] == 'insufficient'
    await queue.aclose()


@pytest.mark.asyncio
async def test_error_keeps_previous_portrait_and_backs_off(database, model, monkeypatch):
    queue = InferenceQueue(idle)
    try:
        assert await refresh(database, Settings(), queue, -1, 42)
        release(database)
        add(database, 11, 20)

        async def fail(*args, **kwargs):
            raise InferenceError('synthetic')

        monkeypatch.setattr(semantic_profiles, 'analyze', fail)
        assert not await refresh(database, Settings(), queue, -1, 42)
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM profile_cards').fetchone()[0] == 2
            assert db.execute('SELECT state FROM automatic_profiles').fetchone()[0] == 'error'
        assert not await refresh(database, Settings(), queue, -1, 42)
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_forget_during_analysis_cannot_save(database, model, monkeypatch):
    analyze = semantic_profiles.analyze

    async def forget_after(*args, **kwargs):
        draft = await analyze(*args, **kwargs)
        forget(database, -1, 42)
        return draft

    monkeypatch.setattr(semantic_profiles, 'analyze', forget_after)
    queue = InferenceQueue(idle)
    try:
        assert not await refresh(database, Settings(), queue, -1, 42)
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM profile_cards').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM automatic_profiles').fetchone()[0] == 0
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_preempted_analysis_does_not_cancel_scheduler(database, monkeypatch):
    async def preempt(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(semantic_profiles, 'analyze', preempt)
    queue = InferenceQueue(idle)
    assert not await refresh(database, Settings(), queue, -1, 42)
    with connect(database) as db:
        assert db.execute('SELECT state FROM automatic_profiles').fetchone()[0] == 'pending'
    await queue.aclose()


@pytest.mark.asyncio
async def test_empty_result_is_explicit_and_does_not_loop(database, monkeypatch):
    async def empty(*args, **kwargs):
        version, rows = semantic_profiles.snapshot(database, -1, 42)
        signature = hashlib.sha256(repr((version, [(r['id'], r['text_hash']) for r in rows])).encode()).hexdigest()
        return {'chat': -1, 'target': 42, 'epoch': version, 'observations': [],
                'created': time.time(), 'signature': signature, 'message_id': 10, 'sampled': 10}

    monkeypatch.setattr(semantic_profiles, 'analyze', empty)
    queue = InferenceQueue(idle)
    assert await refresh(database, Settings(), queue, -1, 42)
    release(database)
    assert not await refresh(database, Settings(), queue, -1, 42)
    with connect(database) as db:
        assert db.execute('SELECT state FROM automatic_profiles').fetchone()[0] == 'empty'
    await queue.aclose()


@pytest.mark.asyncio
async def test_analysis_resumes_current_batch_after_interactive_preemption(database, model):
    class PreemptOnce:
        attempts = 0

        async def submit(self, *, factory, deadline, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise asyncio.CancelledError
            return await factory(deadline)

    queue = PreemptOnce()
    draft = await semantic_profiles.analyze(database, Settings(), -1, 42,
                                             lambda done, total: None, queue=queue)
    assert queue.attempts == 2
    assert len(model) == 1
    assert draft['observations']


async def test_automatic_refresh_retains_old_facts_and_dossier_reaches_roast(database, model):
    from pozor.context import retrieve
    queue = InferenceQueue(idle, background_timeout=60)
    try:
        assert await refresh(database, Settings(), queue, -1, 42)
        with connect(database) as db:
            db.execute("UPDATE profile_cards SET summary='Рассказал о первом шахматном турнире' WHERE profile_version=3")
        release(database)
        add(database, 11, 20)
        assert await refresh(database, Settings(), queue, -1, 42)
        with connect(database) as db:
            assert db.execute("SELECT 1 FROM profile_cards WHERE summary='Рассказал о первом шахматном турнире'").fetchone()
        pack = retrieve(database, chat_id=-1, thread_id=0, target_id=42,
                        trigger_id=100, hint='Шахматы', target_label='Участник')
        assert pack.dossier and pack.dossier[0].evidence_ids
        with connect(database) as db:
            source = int(pack.dossier[0].evidence_ids[0].split(':')[1])
            db.execute("UPDATE messages SET text_hash='changed' WHERE id=?", (source,))
        assert not retrieve(database, chat_id=-1, thread_id=0, target_id=42,
                            trigger_id=100, hint='Шахматы', target_label='Участник').dossier
    finally:
        await queue.aclose()


def test_portrait_does_not_turn_bot_requests_or_single_mentions_into_interests():
    assert not semantic_profiles.profile_material('@synthetic_bot прожарь Макса')
    assert not semantic_profiles.profile_material('ХЩШЭЖЖ')
    assert semantic_profiles.profile_material('Люблю шахматы')
    item = {'kind': 'interest', 'summary': 'Интересуется криптовалютами', 'source_ids': [1],
            'sources': [{'text': 'Криптопровал в чате опять'}]}
    assert not semantic_profiles.grounded_observation(item)
    item['sources'][0]['text'] = 'Люблю играть в шахматы'
    assert semantic_profiles.grounded_observation(item)
