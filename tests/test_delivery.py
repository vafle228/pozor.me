"""T5 acceptance: synthetic imports, real SQLite/queue/validator, fake Telegram."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update

from pozor.commands import close_session, set_consent, start_session
from pozor.config import Settings
from pozor.delivery import DeliveryPipeline, recover_delivery
from pozor.importing import apply_import, preview_import, rollback_import
from pozor.inference_queue import InferenceQueue
from pozor.ingestion import _persist_batch, poll_once, process_pending
from pozor.llm import GenerationResult, Generator, LlamaClient, validate_output
from pozor.storage import connect, migrate


class Clock:
    now = 1793400000.0

    def __call__(self):
        return self.now


class Bot:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return SimpleNamespace(message_id=700 + len(self.calls))

    async def get_chat_member(self, **kwargs):
        return SimpleNamespace(status="member")


class FakeGenerator:
    def __init__(self):
        self.packs = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def generate(self, pack, deadline):
        self.packs.append(pack)
        self.entered.set()
        await self.release.wait()
        ids = [pack.evidence[0].evidence_id] if pack.evidence else [pack.trigger.evidence_id]
        fresh = ["План победы всё ещё на экране загрузки.",
                 "Турнир закончился раньше, чем загрузилась легенда.",
                 "Опоздание стало единственным стабильным рекордом.",
                 "Дедлайн ждал соперника, но увидел только будильник."]
        raw = json.dumps({"text": fresh[(len(self.packs) - 1) % len(fresh)],
                          "evidence_ids": ids, "angle": "synthetic"})
        return GenerationResult(roast=validate_output(raw, pack), fallback=False, context_hash="test")


async def idle(deadline):
    pass


@pytest.fixture
def scope(tmp_path):
    database = tmp_path / "delivery.sqlite3"
    migrate(database)
    with connect(database) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1001,1,1)")
        db.execute("INSERT INTO members(chat_id,user_id,consent) VALUES (-1001,42,1),(-1001,43,1)")
    clock, bot, generator = Clock(), Bot(), FakeGenerator()
    pipeline = DeliveryPipeline(database, Settings(_env_file=None), bot, generator,
                                InferenceQueue(idle), clock=clock)
    pipeline.ready = True
    return pipeline, database, clock, bot, generator


def start(scope):
    p, db, clock, _, _ = scope
    sid = start_session(db, chat_id=-1001, thread_id=0, target_id=42, initiator_id=43,
                        trigger_message_id=1, now=int(clock()))
    assert p.accept(sid, 1, "игра reddit", first=True)
    return sid


async def generated(p):
    await asyncio.gather(*list(p.jobs.values()))


async def test_real_aiogram_update_reaches_command_pipeline(scope):
    pipeline, database, clock, bot, _ = scope
    with connect(database) as db:
        db.execute('UPDATE chats SET enabled=0')
    update = Update.model_validate({
        'update_id': 900, 'message': {
            'message_id': 900, 'date': int(clock()),
            'chat': {'id': -1001, 'type': 'supergroup', 'title': 'Synthetic'},
            'from': {'id': 42, 'is_bot': False, 'first_name': 'Synthetic'},
            'text': '/enable_chat',
            'entities': [{'type': 'bot_command', 'offset': 0, 'length': 12}],
        },
    })

    class Source:
        async def get_updates(self, **kwargs):
            return [update]

    async def handler(payload):
        await pipeline.handle_update(payload, bot_id=99, bot_username='synthetic_bot',
                                     owner_ids=frozenset({42}))

    await poll_once(Source(), database, 99, control_handler=handler)
    assert bot.calls[-1]['text'] == 'Чат включён.'
    with connect(database) as db:
        assert db.execute('SELECT enabled FROM chats').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM command_receipts').fetchone()[0] == 1
    await pipeline.aclose()


def state(database, table="outbox"):
    with connect(database) as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]


def imported(database, tmp_path):
    file = tmp_path / "synthetic.json"
    file.write_text(json.dumps({"id": 9001, "type": "private_group", "messages": [
        {"id": 11, "type": "message", "date_unixtime": "1767225600",
         "from_id": "user42", "text": "reddit игра обещаю победу"}
    ]}), encoding="utf-8")
    preview = preview_import(database, file_path=file, format="telegram-desktop", source_chat_id=9001,
                             chat_id=-1001, target_id=42, from_date="2026-01-01", until_date="2026-01-02",
                             manifest_path=tmp_path / "preview.json")
    apply_import(database, preview.manifest_path)
    return preview.batch_id


@pytest.mark.asyncio
async def test_vertical_import_generate_deliver_stop(scope, tmp_path):
    p, db, _, bot, generator = scope
    imported(db, tmp_path)
    sid = start(scope)
    await generated(p)
    assert generator.packs[0].evidence[0].attribution == "imported_quote"
    assert await p.send_once()
    assert state(db)[0]["status"] == "sent"
    assert state(db, "sessions")[0]["turn_count"] == 1
    assert bot.calls[0]["parse_mode"] is None
    assert bot.calls[0]["reply_parameters"].message_id == 1
    assert state(db, "turns")[0]["evidence_ids"] != "[]"
    assert close_session(db, session_id=sid, chat_id=-1001, thread_id=0, actor_id=42)
    assert not await p.send_once()
    await p.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["stop", "edit", "forget_epoch", "rollback", "opt_out", "disable", "deadline"])
@pytest.mark.parametrize("stage", ["inference", "outbox"])
async def test_stale_results_never_send(scope, tmp_path, cause, stage):
    p, db, clock, bot, generator = scope
    batch = imported(db, tmp_path)
    if stage == "inference":
        generator.release.clear()
    sid = start(scope)
    await generator.entered.wait()
    if stage == "outbox":
        await generated(p)
    if cause == "stop":
        close_session(db, session_id=sid, chat_id=-1001, thread_id=0, actor_id=42)
    elif cause == "opt_out":
        set_consent(db, -1001, 42, False)
    elif cause == "rollback":
        rollback_import(db, batch)
    elif cause == "deadline":
        clock.now += 9
    else:
        with connect(db) as con:
            if cause == "disable":
                con.execute("UPDATE chats SET enabled=0")
            else:
                # The T1 edit/T6 forget contract is an epoch increase in the same transaction.
                con.execute("UPDATE data_epochs SET epoch=epoch+1")
    generator.release.set()
    await generated(p)
    p.invalidate()
    await p.send_once()
    assert bot.calls == []
    await p.aclose()


@pytest.mark.asyncio
async def test_429_retry_rechecks_guards(scope):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)
    bot.failure = TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="test"),
                                     message="synthetic", retry_after=2)
    await p.send_once()
    assert state(db)[0]["status"] == "ready"
    assert not await p.send_once()
    bot.failure = None
    clock.now += 3
    assert await p.send_once()
    assert state(db)[0]["status"] == "sent"
    assert len(bot.calls) == 2
    assert not await p.send_once()
    await p.aclose()


@pytest.mark.asyncio
async def test_429_then_stop_never_retries(scope):
    p, db, clock, bot, _ = scope
    sid = start(scope)
    await generated(p)
    bot.failure = TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="test"),
                                     message="synthetic", retry_after=1)
    await p.send_once()
    close_session(db, session_id=sid, chat_id=-1001, thread_id=0, actor_id=42)
    clock.now += 3
    await p.send_once()
    assert len(bot.calls) == 1
    assert state(db)[0]["status"] == "cancelled"
    await p.aclose()


@pytest.mark.asyncio
async def test_ambiguous_timeout_is_terminal(scope):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)
    bot.failure = TimeoutError()
    await p.send_once()
    assert state(db)[0]["status"] == "unknown"
    clock.now += 3
    recover_delivery(db)
    assert not await p.send_once()
    assert len(bot.calls) == 1
    await p.aclose()


@pytest.mark.asyncio
async def test_crash_after_telegram_success_before_commit(scope, monkeypatch):
    p, db, _, bot, _ = scope
    start(scope)
    await generated(p)

    def crash(*args):
        raise RuntimeError("simulated process loss")

    monkeypatch.setattr(p, "_finish_send", crash)
    with pytest.raises(RuntimeError):
        await p.send_once()
    assert state(db)[0]["status"] == "sending"
    recover_delivery(db)
    assert state(db)[0]["status"] == "unknown"
    assert not await p.send_once()
    assert len(bot.calls) == 1
    await p.aclose()


@pytest.mark.asyncio
async def test_forbidden_disables_chat(scope):
    p, db, _, bot, _ = scope
    start(scope)
    await generated(p)
    bot.failure = TelegramForbiddenError(method=SendMessage(chat_id=-1001, text="test"), message="synthetic")
    await p.send_once()
    assert state(db)[0]["error_code"] == "403"
    assert not state(db, "chats")[0]["enabled"]
    assert state(db, "sessions")[0]["state"] == "closed"
    await p.aclose()


def incoming(clock, mid=2, *, actor=42, reply=701, thread=0, chat=-1001, text="игра ответ", **extra):
    return {"message": {"message_id": mid, "date": int(clock()), "text": text,
                        "chat": {"id": chat, "type": "supergroup"}, "message_thread_id": thread, "is_topic_message": bool(thread),
                        "from": {"id": actor, "first_name": "Synthetic"},
                        "reply_to_message": {"message_id": reply}, **extra}}


@pytest.mark.asyncio
async def test_direct_reply_only_coalescing_memory_and_limit(scope):
    p, db, clock, bot, generator = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    for extra in ({"reply": 999}, {"actor": 44}, {"thread": 8}, {"chat": -2002}):
        await p.handle_message(incoming(clock, **extra), "pozor_me_bot")
    assert len(state(db, "turns")) == 1
    for n in range(2, 5):
        clock.now += 11
        await p.handle_message(incoming(clock, mid=n * 10, reply=700 + len(bot.calls)), "pozor_me_bot")
        await p.handle_message(incoming(clock, mid=n * 10 + 1, reply=700 + len(bot.calls), text="ещё ответ"), "pozor_me_bot")
        # Fake debounce clock without waiting ten seconds in the test suite.
        p.due[sid] = (0, 0)
        await generated(p)
        assert "ещё ответ" in generator.packs[-1].trigger.text
        assert len(generator.packs[-1].session_turns) == n - 1
        await p.send_once()
    assert len(bot.calls) == 4
    assert state(db, "sessions")[0]["state"] == "closed"
    assert not p.accept(sid, 99, "пятый", first=True)
    assert not await p.send_once()
    await p.aclose()


@pytest.mark.asyncio
async def test_new_reply_during_inference_replaces_revision(scope):
    p, db, clock, bot, generator = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    generator.entered.clear()
    generator.release.clear()
    clock.now += 11
    assert p.accept(sid, 2, "второй", first=True)
    await generator.entered.wait()
    assert p.accept(sid, 3, "третий", first=True)
    generator.release.set()
    await generated(p)
    assert len(generator.packs) == 3
    assert "третий" in generator.packs[-1].trigger.text
    await p.send_once()
    assert len(bot.calls) == 2
    assert len(state(db)) == 2
    await p.aclose()


@pytest.mark.asyncio
async def test_stop_during_external_send_records_success_but_no_next_turn(scope):
    p, db, _, bot, _ = scope
    sid = start(scope)
    await generated(p)
    entered, release = asyncio.Event(), asyncio.Event()

    async def in_flight(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=701)

    bot.send_message = in_flight
    sending = asyncio.create_task(p.send_once())
    await entered.wait()
    close_session(db, session_id=sid, chat_id=-1001, thread_id=0, actor_id=42)
    release.set()
    await sending
    assert state(db)[0]["status"] == "sent"
    assert state(db, "sessions")[0]["state"] == "closed"
    assert not p.accept(sid, 2, "ещё", first=True)
    await p.aclose()


@pytest.mark.asyncio
async def test_offline_http_generator_to_production_sender(scope, tmp_path):
    from pozor.operations import backup, restore

    p, db, _, bot, _ = scope
    imported(db, tmp_path)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        data = json.loads(request.content) if request.content else {}
        if request.url.path == "/slots":
            result = [{"is_processing": False}]
        elif request.url.path == "/tokenize":
            result = {"tokens": [1, 2, 3]}
        elif request.url.path == "/apply-template":
            result = {"prompt": "synthetic rendered"}
        else:
            pack = json.loads(data["messages"][-1]["content"])
            assert pack["evidence"][0]["attribution"] == "imported_quote"
            result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "text": "Твой план победы всё ещё загружается.",
                "evidence_ids": [pack["evidence"][0]["evidence_id"]], "angle": "game"})}}]}
        return httpx.Response(200, json=result)

    async with LlamaClient(p.settings, transport=httpx.MockTransport(handler)) as client:
        p.generator = Generator(client)
        p.queue = InferenceQueue(client.wait_idle)
        sid = start(scope)
        await generated(p)
        await p.send_once()
        assert "/v1/chat/completions" in paths
        assert state(db)[0]["status"] == "sent"
        assert "загружается" in bot.calls[0]["text"]
        await p.aclose()
        # T7: actual import + HTTP adapter + sender state survives backup/restore.
        snapshot = backup(db, tmp_path / 'snapshots')
        restored = tmp_path / 'restored.sqlite3'
        restore(snapshot, restored)
        restarted = DeliveryPipeline(restored, p.settings, bot, Generator(client),
                                     InferenceQueue(client.wait_idle))
        try:
            restarted.ready = True
            assert not restarted.accept(sid, 2, 'игра reddit', first=True)
            assert not await restarted.send_once()
            assert len(bot.calls) == 1
            assert state(restored)[0]['status'] == 'sent'
            assert state(restored, 'sessions')[0]['closed_reason'] == 'restart'
            with connect(restored) as con:
                assert con.execute("SELECT count(*) FROM messages_fts "
                                   "WHERE messages_fts MATCH 'reddit'").fetchone()[0] == 1
                assert con.execute("SELECT count(*) FROM imports "
                                   "WHERE status='active'").fetchone()[0] == 1
        finally:
            await restarted.aclose()


@pytest.mark.asyncio
async def test_unavailable_health_and_stale_commands_do_not_start(scope):
    p, db, clock, bot, _ = scope
    p.ready = False
    for mid in (1, 2):
        await p.handle_update(incoming(clock, mid, actor=43, text="/roast"), bot_id=99,
                              bot_username="pozor_me_bot", owner_ids=frozenset())
    assert len(bot.calls) == 1
    assert state(db, "sessions") == []
    p.ready = True
    payload = incoming(clock, actor=43, text="/roast")
    payload["message"]["date"] -= 31
    await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    assert state(db, "sessions") == []
    await p.aclose()


@pytest.mark.asyncio
async def test_polling_commands_import_and_stop_while_gpu_busy(scope, tmp_path, monkeypatch):
    from pozor import commands

    p, db, clock, bot, generator = scope
    monkeypatch.setattr(commands, "utc_now", lambda: int(clock()))
    imported(db, tmp_path)
    generator.release.clear()
    command = incoming(clock, mid=1, actor=43, text="/roast reddit")
    command["update_id"] = 1
    command["message"]["reply_to_message"]["from"] = {"id": 42}

    class Source:
        async def get_updates(self, **kwargs):
            return [command]

    async def handler(payload):
        await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())

    await poll_once(Source(), db, 99, control_handler=handler)
    await generator.entered.wait()
    assert generator.packs[0].evidence[0].attribution == "imported_quote"
    assert state(db, "inbox")[0]["normalized_payload"] == ""
    sid = state(db, "sessions")[0]["id"]
    # /stop is processed while fake completion is still blocked.
    await handler(incoming(clock, mid=2, text="/stop"))
    assert state(db, "sessions")[0]["state"] == "closed"
    assert not p.accept(sid, 3, "повтор", first=True)
    generator.release.set()
    await generated(p)
    assert not await p.send_once()
    assert len(bot.calls) == 2  # command ACK and stop ACK; no roast
    await p.aclose()


@pytest.mark.parametrize('argument,expected', [('', 'Дружеская прожарка участника.'),
                                               ('игра', 'игра'), ('@known_user игра', 'Участник игра')])
async def test_roast_routing_suffix_is_not_model_evidence(scope, monkeypatch, argument, expected):
    from pozor import commands

    pipeline, database, clock, _bot, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    payload = incoming(clock, mid=1, actor=43, text='/roast@pozor_me_bot ' + argument)
    payload['message']['reply_to_message'] = {
        'message_id': 50, 'date': int(clock()), 'chat': {'id': -1001, 'type': 'supergroup'},
        'from': {'id': 42}, 'text': 'Обещал выиграть турнир, но проспал начало.'}
    if argument.startswith('@'):
        with connect(database) as db:
            db.execute("INSERT INTO aliases(chat_id,user_id,username,valid_from,source) "
                       "VALUES (-1001,42,'known_user','2020-01-01','live')")
        payload['message']['entities'] = [
            {'type': 'mention', 'offset': len('/roast@pozor_me_bot '), 'length': 11}]
    await pipeline.handle_update(payload, bot_id=99, bot_username='pozor_me_bot',
                                 owner_ids=frozenset())
    await generated(pipeline)
    quote = 'Реплика участника: Обещал выиграть турнир, но проспал начало.'
    assert generator.packs[0].trigger.text == ((argument and expected + '\n') or '') + quote
    assert '@pozor_me_bot' not in generator.packs[0].trigger.text
    await pipeline.aclose()


async def test_empty_roast_refuses_session_without_charging_cooldown(scope, monkeypatch):
    from pozor import commands

    pipeline, database, clock, bot, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    payload = incoming(clock, mid=1, actor=43, text='/roast')
    payload['message']['reply_to_message']['from'] = {'id': 42}
    await pipeline.handle_update(payload, bot_id=99, bot_username='pozor_me_bot',
                                 owner_ids=frozenset())
    assert 'Пока нет материала' in bot.calls[-1]['text']
    assert state(database, 'sessions') == []
    assert state(database, 'command_cooldowns') == []
    assert not generator.packs
    await pipeline.aclose()


@pytest.mark.parametrize("command", ["/roast", "@pozor_me_bot", "@pozor_me_bot попусти"])
async def test_reply_text_reaches_real_context_before_generation(scope, monkeypatch, command):
    from pozor import commands

    pipeline, _database, clock, _, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    quote = 'Обещал выиграть турнир, но проспал начало.'
    payload = incoming(clock, mid=1, actor=43, text=command)
    if command.startswith("@"):
        payload["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 13}]
    payload['message']['reply_to_message'] = {
        'message_id': 50, 'date': int(clock()), 'chat': {'id': -1001, 'type': 'supergroup'},
        'from': {'id': 42}, 'text': quote}
    await pipeline.handle_update(payload, bot_id=99, bot_username='pozor_me_bot',
                                 owner_ids=frozenset())
    await generated(pipeline)
    pack = generator.packs[0]
    evidence = [e for e in [*pack.recent, *pack.evidence] if e.text == quote]
    assert len(evidence) == 1
    assert evidence[0].author_id == 42 and evidence[0].attribution == 'observed_self'
    await pipeline.aclose()


@pytest.mark.asyncio
async def test_real_ingestion_edit_invalidates_ready_result(scope):
    p, db, clock, bot, _ = scope
    row = incoming(clock, mid=10, text="игра обещаю выиграть")
    row["update_id"] = 10
    _persist_batch(db, 99, [row])
    process_pending(db)
    start(scope)
    await generated(p)
    edit = {"update_id": 11, "edited_message": row["message"] | {"text": "игра передумал"}}
    _persist_batch(db, 99, [edit])
    process_pending(db)
    await p.send_once()
    assert bot.calls == []
    assert state(db)[0]["status"] == "cancelled"
    await p.aclose()


@pytest.mark.asyncio
async def test_edit_trigger_cancels_even_without_subject_epoch(scope):
    p, db, _, bot, _ = scope
    start(scope)
    await generated(p)
    await p.handle_message({"edited_message": {"message_id": 1, "chat": {"id": -1001}}}, None)
    await p.send_once()
    assert bot.calls == []
    assert state(db)[0]["status"] == "cancelled"
    await p.aclose()


@pytest.mark.asyncio
async def test_fast_reply_defers_and_mentions_stop(scope):
    p, db, clock, bot, _ = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 1
    await p.handle_message(incoming(clock), "pozor_me_bot")
    assert p.due[sid][1] - asyncio.get_running_loop().time() > 8
    assert not await p.send_once()
    text = "@pozor_me_bot хватит"
    await p.handle_message(incoming(clock, mid=3, reply=999, text=text,
                                    entities=[{"type": "mention", "offset": 0, "length": 13}]),
                           "pozor_me_bot")
    # Bot username is 12 characters plus @ (UTF-16 length 13).
    assert state(db, "sessions")[0]["state"] == "closed"
    assert len(bot.calls) == 1
    await p.aclose()


@pytest.mark.parametrize("actor", [42, 43])
async def test_participant_bot_mention_continues_then_stops_without_new_session(scope, actor):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 10
    payload = incoming(clock, mid=2, actor=actor, reply=999, text="@pozor_me_bot вот ответ",
                       entities=[{"type": "mention", "offset": 0, "length": 13}])
    await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    await generated(p)
    assert len(state(db, "sessions")) == 1
    assert len(bot.calls) == 1  # no command error or second session acknowledgement
    assert len(state(db, "turns")) == 2
    payload = incoming(clock, mid=3, reply=999, text="@pozor_me_bot хватит",
                       entities=[{"type": "mention", "offset": 0, "length": 13}])
    await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    assert state(db, "sessions")[0]["state"] == "closed"
    assert len(bot.calls) == 1
    await p.aclose()


async def test_initiator_reply_continues_with_memory(scope):
    p, db, clock, bot, generator = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 11
    await p.handle_update(incoming(clock, actor=43, text="а про турнир расскажи"),
                          bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    p.due[sid] = (0, 0)
    await generated(p)
    assert len(generator.packs[-1].session_turns) == 1
    assert "про турнир" in generator.packs[-1].trigger.text
    assert await p.send_once()
    assert len(bot.calls) == 2
    assert len(state(db, "sessions")) == 1
    await p.aclose()


async def test_explicit_mention_target_replaces_session_and_cancels_old_answer(scope, monkeypatch):
    from pozor import commands

    p, db, clock, bot, _ = scope
    monkeypatch.setattr(commands, "utc_now", lambda: int(clock()))
    sid = start(scope)
    await generated(p)  # Old punch is ready but has not been sent.
    clock.now += 11
    payload = incoming(clock, actor=43, text="@pozor_me_bot прожарь турнир")
    payload["message"]["entities"] = [{"type": "mention", "offset": 0, "length": 13}]
    payload["message"]["reply_to_message"] = {
        "message_id": 50, "from": {"id": 43}, "text": "Опять проспал турнир."}
    await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    await generated(p)
    assert state(db, "sessions")[0]["closed_reason"] == "replaced"
    assert len(state(db, "sessions")) == 2
    assert await p.send_once()
    assert all(row["status"] == "cancelled" for row in state(db) if row["session_id"] == sid)
    assert len(bot.calls) == 2  # New command acknowledgement and new punch only.
    await p.aclose()


@pytest.mark.asyncio
async def test_restart_closes_pending_and_preserves_sent(scope):
    p, db, _, _, _ = scope
    start(scope)
    await generated(p)
    recover_delivery(db)
    assert state(db)[0]["status"] == "cancelled"
    assert state(db, "sessions")[0]["closed_reason"] == "restart"
    assert not await p.send_once()
    assert migrate(db) == []
    with connect(db) as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    await p.aclose()


def test_mention_call_preserves_entity_target_identity():
    from pozor.commands import parse_command

    text = "@pozor_me_bot попусти @target_one reddit"
    parsed = parse_command(text, ({"type": "mention", "offset": 0, "length": 13},
                                  {"type": "mention", "offset": 22, "length": 11}),
                           bot_username="pozor_me_bot")
    assert parsed.name == "roast"
    assert parsed.username_targets == ("target_one",)


@pytest.mark.asyncio
async def test_rebuild_loop_stops_after_two_replacements(scope):
    p, db, _, bot, generator = scope
    generator.release.clear()
    sid = start(scope)
    await generator.entered.wait()
    for mid in (2, 3):
        generator.entered.clear()
        assert p.accept(sid, mid, f"ответ {mid}", first=True)
        await generator.entered.wait()
    assert not p.accept(sid, 4, "непрерывный поток", first=True)
    generator.release.set()
    await generated(p)
    assert len(generator.packs) == 3
    assert state(db, "turns")[0]["status"] == "cancelled"
    assert not await p.send_once()
    assert bot.calls == []
    # A later, new direct address can start another bounded attempt.
    assert p.accept(sid, 5, "новое обращение", first=True)
    await generated(p)
    await p.send_once()
    assert len(bot.calls) == 1
    await p.aclose()


@pytest.mark.asyncio
async def test_global_and_chat_delivery_limits_are_durable(scope):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)
    with connect(db) as con:
        con.execute("INSERT INTO sessions(id,chat_id,thread_id,target_id,initiator_id,state,created_at,"
                    "trigger_message_id) VALUES ('second_12345',-1001,8,43,42,'active',?,99)", (int(clock()),))
    assert p.accept("second_12345", 99, "вторая тема", first=True)
    await generated(p)
    await p.send_once()
    assert not await p.send_once()
    clock.now += 1.1  # global limit has expired, chat limit has not
    assert not await p.send_once()
    clock.now += 2
    assert await p.send_once()
    assert len(bot.calls) == 2
    assert all(row["status"] == "sent" for row in state(db))
    assert len(state(db, "delivery_limits")) == 2
    await p.aclose()


@pytest.mark.asyncio
async def test_simultaneous_sender_calls_claim_only_once(scope):
    p, db, _, bot, _ = scope
    start(scope)
    await generated(p)
    await asyncio.gather(p.send_once(), p.send_once())
    assert len(bot.calls) == 1
    assert state(db, "sessions")[0]["turn_count"] == 1
    await p.aclose()


@pytest.mark.asyncio
async def test_queue_busy_before_start_does_not_open_session(scope):
    p, db, clock, bot, _ = scope
    # Fill the actual bounded queue behind a blocked GPU operation.
    p.queue.capacity = 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(deadline):
        entered.set()
        await release.wait()

    deadline = asyncio.get_running_loop().time() + 8
    active = asyncio.create_task(p.queue.submit(key="active", chat_id=-2, deadline=deadline, factory=blocked))
    await entered.wait()
    pending = asyncio.create_task(p.queue.submit(key="pending", chat_id=-3, deadline=deadline, factory=blocked))
    await asyncio.sleep(0)
    await p.handle_update(incoming(clock, actor=43, text="/roast"), bot_id=99,
                          bot_username="pozor_me_bot", owner_ids=frozenset())
    assert state(db, "sessions") == []
    assert "занят" in bot.calls[0]["text"]
    release.set()
    await asyncio.gather(active, pending)
    await p.aclose()


@pytest.mark.asyncio
async def test_retrieval_timeout_is_terminal(scope, monkeypatch):
    from pozor import delivery

    p, db, _, bot, generator = scope
    attempts = []

    def timed_out(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError

    monkeypatch.setattr(delivery, "retrieve", timed_out)
    start(scope)
    try:
        await asyncio.wait_for(generated(p), 2)
        assert attempts == [1]
        assert state(db, "turns")[0]["status"] == "failed"
        assert state(db, "turns")[0]["deadline"] is not None
        assert generator.packs == []
        assert not await p.send_once()
        assert len(bot.calls) == 1
        assert 'Раунд сохранён' in bot.calls[0]['text']
        assert state(db, 'turns')[0]['generation_reason'] == 'deadline'
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_edited_input_is_not_reused_on_next_address(scope):
    p, db, clock, _, generator = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 11
    assert p.accept(sid, 2, "текст до редактирования", first=True)
    await generated(p)
    await p.handle_message({"edited_message": {"message_id": 2, "chat": {"id": -1001}}}, None)
    assert state(db, "turns")[-1]["status"] == "cancelled"
    assert p.accept(sid, 3, "новое обращение", first=True)
    await generated(p)
    assert generator.packs[-1].trigger.text == "новое обращение"
    assert await p.send_once()
    await p.aclose()


@pytest.mark.asyncio
async def test_chat_cooldown_does_not_block_other_chats(scope):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)
    with connect(db) as con:
        con.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-2002,1,1)")
        con.execute("INSERT INTO members(chat_id,user_id,consent) VALUES (-2002,42,1)")
        con.execute("INSERT INTO delivery_limits VALUES ('chat:-1001',?)", (clock() + 3,))
    sid = start_session(db, chat_id=-2002, thread_id=0, target_id=42, initiator_id=43,
                        trigger_message_id=1, now=int(clock()))
    assert p.accept(sid, 1, "другая группа", first=True)
    await generated(p)
    assert await p.send_once()
    assert [call["chat_id"] for call in bot.calls] == [-2002]
    assert state(db)[0]["status"] == "ready"
    await p.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "unlisted", "private", "bot", "anonymous"])
async def test_unavailable_notice_respects_scope_and_sender(scope, case):
    p, db, clock, bot, _ = scope
    p.ready = False
    payload = incoming(clock, text="/roast", actor=43)
    if case in {"disabled", "unlisted"}:
        with connect(db) as con:
            con.execute("UPDATE chats SET " + ("enabled" if case == "disabled" else "allowed") + "=0")
    elif case == "private":
        payload["message"]["chat"]["type"] = "private"
    elif case == "bot":
        payload["message"]["from"]["is_bot"] = True
    else:
        payload["message"]["sender_chat"] = {"id": -1001}
    await p.handle_update(payload, bot_id=99, bot_username="pozor_me_bot", owner_ids=frozenset())
    assert bot.calls == []
    assert state(db, "sessions") == []
    await p.aclose()


@pytest.mark.parametrize("entities", [(), ({"type": "mention", "offset": 0, "length": 3},)])
def test_mention_command_requires_bot_entity(entities):
    from pozor.commands import parse_command

    assert parse_command("@pozor_me_bot попусти @target_one", entities,
                         bot_username="pozor_me_bot") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [42, 43])
@pytest.mark.parametrize("command", ["/stop", "/finish"])
async def test_participant_stop_never_waits_for_admin_lookup(scope, actor, command):
    p, db, clock, bot, _ = scope
    start(scope)
    await generated(p)

    async def unavailable(**kwargs):
        pytest.fail("Participant stop must not need a network authorization lookup")

    bot.get_chat_member = unavailable
    await p.handle_update(incoming(clock, actor=actor, text=command), bot_id=99,
                          bot_username="pozor_me_bot", owner_ids=frozenset())
    assert state(db, "sessions")[0]["state"] == "closed"
    assert not await p.send_once()
    await p.aclose()


@pytest.mark.asyncio
async def test_forget_during_inference_purges_and_cancels_without_resurrection(scope):
    p, db, clock, bot, generator = scope
    generator.release.clear()
    start(scope)
    await generator.entered.wait()
    await p.handle_update(incoming(clock, actor=42, mid=2, text='/forget_me'),
                          bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    generator.release.set()
    await generated(p)
    assert state(db, 'turns') == state(db, 'turn_inputs') == state(db, 'outbox') == []
    assert not await p.send_once()
    assert len(bot.calls) == 1 and 'удалены' in bot.calls[0]['text']
    await p.aclose()


@pytest.mark.parametrize("text,mode", [
    ("@pozor_me_bot как дела?", "dialogue"),
    ("@pozor_me_bot", "dialogue"),
    ("@pozor_me_bot прожарь меня за опоздание", "roast"),
    ("/roast меня за опоздание", "roast"),
])
async def test_self_roast_and_targetless_dialogue(scope, monkeypatch, text, mode):
    from pozor import commands
    from pozor.llm import messages_for

    p, database, clock, bot, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    payload = incoming(clock, mid=1, actor=43, text=text)
    payload['message'].pop('reply_to_message', None)
    if text.startswith('@'):
        payload['message']['entities'] = [{'type': 'mention', 'offset': 0, 'length': 13}]
    await p.handle_update(payload, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    await generated(p)
    session = state(database, 'sessions')[0]
    assert session['target_id'] == session['initiator_id'] == 43
    assert session['mode'] == generator.packs[0].mode == mode
    assert ('Это диалог' in messages_for(generator.packs[0])[0]['content']) == (mode == 'dialogue')
    assert len(bot.calls) == (0 if mode == 'dialogue' else 1)
    assert await p.send_once()
    if mode == 'dialogue':
        clock.now += 11
        reply = incoming(clock, mid=2, actor=43, text='А почему?',
                         reply=bot.calls[-1].get('message_id', 701))
        # Fake Telegram returns IDs starting at 701.
        reply['message']['reply_to_message']['message_id'] = 701
        await p.handle_update(reply, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
        p.due[session['id']] = (0, 0)
        await generated(p)
        memory = generator.packs[-1].session_turns
        assert len(memory) == 1
        conversation = generator.packs[-1].conversation
        assert [entry.role for entry in conversation] == ['user', 'assistant']
        assert conversation[0].author_id == 43
        assert conversation[1].text == memory[0]
        assert generator.packs[-1].trigger.text == 'А почему?'
    await p.aclose()


async def test_self_roast_still_requires_consent(scope, monkeypatch):
    from pozor import commands

    p, database, clock, bot, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    with connect(database) as db:
        db.execute('UPDATE members SET consent=0 WHERE user_id=43')
    payload = incoming(clock, actor=43, text='/roast меня за опоздание')
    payload['message'].pop('reply_to_message', None)
    await p.handle_update(payload, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    assert '/roast_on' in bot.calls[-1]['text']
    assert not state(database, 'sessions')
    assert not generator.packs
    await p.aclose()


@pytest.mark.parametrize("fallback", [False, True])
async def test_question_during_roast_preserves_mode_and_replies_to_question(scope, fallback):
    from pozor.llm import DIALOGUE_FALLBACK, GeneratedRoast

    p, database, clock, bot, generator = scope
    sid = start(scope)
    await generated(p)
    assert await p.send_once()
    clock.now += 11
    if fallback:
        class FailedGenerator:
            async def generate(self, pack, deadline):
                generator.packs.append(pack)
                return GenerationResult(roast=GeneratedRoast(text=DIALOGUE_FALLBACK,
                                        evidence_ids=[], angle='fallback'), fallback=True)
        p.generator = FailedGenerator()
    payload = incoming(clock, mid=25, actor=43, reply=701,
                       text='@pozor_me_bot как дела?',
                       entities=[{'type': 'mention', 'offset': 0, 'length': 13}])
    await p.handle_update(payload, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    p.due[sid] = (0, 0)
    await generated(p)
    assert generator.packs[-1].mode == 'roast'
    assert generator.packs[-1].trigger.text == 'как дела?'
    assert state(database, 'sessions')[0]['mode'] == 'roast'
    assert await p.send_once()
    reply = bot.calls[-1]['reply_parameters']
    assert reply.message_id == 25
    assert reply.allow_sending_without_reply is False
    if fallback:
        assert bot.calls[-1]['text'] == DIALOGUE_FALLBACK
    await p.aclose()


@pytest.mark.parametrize('address', ['tag', 'tag_target', 'reply_target'])
async def test_same_target_repeated_address_keeps_round_and_authorship(scope, monkeypatch, address):
    from pozor import commands
    p, database, clock, bot, generator = scope
    monkeypatch.setattr(commands, 'utc_now', lambda: int(clock()))
    with connect(database) as db:
        db.execute("UPDATE members SET current_username='known_user',display_name='Макс' WHERE user_id=42")
    sid = start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 11
    text = '@pozor_me_bot продолжай про Reddit'
    payload = incoming(clock, mid=25, actor=43, text=text)
    payload['message']['entities'] = [{'type': 'mention', 'offset': 0, 'length': 13}]
    if address == 'tag_target':
        payload['message']['text'] = '@pozor_me_bot @known_user продолжай про Reddit'
        payload['message']['entities'].append({'type': 'mention', 'offset': 14, 'length': 11})
    if address == 'reply_target':
        payload['message']['reply_to_message'] = {'message_id': 10, 'from': {'id': 42}}
    await p.handle_update(payload, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    p.due[sid] = (0, 0)
    await generated(p)
    assert len(state(database, 'sessions')) == 1
    pack = generator.packs[-1]
    assert pack.target_id == 42 and pack.speaker_id == pack.initiator_id == 43
    assert pack.trigger.author_id == 43 and pack.target_label == 'Макс'
    assert pack.mode == 'roast'
    assert await p.send_once()
    assert '2/4' in bot.calls[-1]['reply_markup'].inline_keyboard[0][1].text
    await p.aclose()


async def test_delivery_has_time_after_generation_finishes_near_deadline(scope):
    p, database, clock, _, generator = scope
    generate = generator.generate
    async def slow(pack, deadline):
        result = await generate(pack, deadline)
        clock.now += 7.5
        return result
    generator.generate = slow
    start(scope)
    await generated(p)
    clock.now += 2
    assert await p.send_once()
    assert state(database, 'turns')[0]['status'] == 'sent'
    await p.aclose()


async def test_reply_after_closed_round_explains_state(scope):
    p, database, clock, bot, _ = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    close_session(database, session_id=sid, chat_id=-1001, thread_id=0, actor_id=42)
    await p.handle_update(incoming(clock, actor=43), bot_id=99,
                          bot_username='pozor_me_bot', owner_ids=frozenset())
    assert 'раунд завершён' in bot.calls[-1]['text']
    assert len(state(database, 'sessions')) == 1
    await p.aclose()


async def test_nonforum_reply_thread_id_does_not_lose_active_round(scope):
    p, database, clock, _, generator = scope
    sid = start(scope)
    await generated(p)
    await p.send_once()
    clock.now += 11
    payload = incoming(clock, mid=25, actor=43, thread=98765, reply=701)
    payload['message']['is_topic_message'] = False
    await p.handle_update(payload, bot_id=99, bot_username='pozor_me_bot', owner_ids=frozenset())
    p.due[sid] = (0, 0)
    await generated(p)
    assert len(generator.packs) == 2
    assert generator.packs[-1].thread_id == 0
    assert len(state(database, 'sessions')) == 1
    await p.aclose()
