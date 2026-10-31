"""T5 acceptance: synthetic imports, real SQLite/queue/validator, fake Telegram."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

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
        raw = json.dumps({"text": f"План победы номер {len(self.packs)} всё ещё загружается.",
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
                        "chat": {"id": chat, "type": "supergroup"}, "message_thread_id": thread,
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
        start(scope)
        await generated(p)
        await p.send_once()
        assert "/v1/chat/completions" in paths
        assert state(db)[0]["status"] == "sent"
        assert "загружается" in bot.calls[0]["text"]
        await p.aclose()


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
