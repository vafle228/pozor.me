from __future__ import annotations

from pathlib import Path

import pytest

from pozor.commands import (
    CommandContext,
    IncomingEvent,
    SessionClock,
    callback_action,
    callback_data,
    callback_session_id,
    close_session,
    close_sessions_on_startup,
    decide_incoming,
    expire_sessions,
    parse_command,
    resolve_target,
    set_chat_enabled,
    set_consent,
    start_session,
    whois,
)
from pozor.storage import connect, migrate


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "commands.sqlite3"
    migrate(path)
    with connect(path) as connection:
        connection.execute("INSERT INTO chats(chat_id, allowed, enabled) VALUES (-1001, 1, 1)")
        connection.execute(
            """INSERT INTO members(chat_id,user_id,display_name,current_username,consent)
               VALUES (-1001, 17, 'Макс', 'new_name', 1),
                      (-1001, 18, 'Инициатор', 'caller', 1)"""
        )
        connection.execute(
            """INSERT INTO aliases(chat_id,user_id,username,valid_from,source)
               VALUES (-1001,17,'old_name','2026-01-01T00:00:00+00:00','live'),
                     (-1001,17,'new_name','2026-02-01T00:00:00+00:00','live')"""
        )
    return path


def test_parser_slices_entity_after_astral_emoji_in_utf16() -> None:
    text = "/roast 😀 @new_name"
    # /roast + space + emoji + space is 10 UTF-16 units; the entity begins at @.
    parsed = parse_command(text, ({"type": "mention", "offset": 10, "length": 9},))
    assert parsed is not None
    assert parsed.username_targets == ("new_name",)


def test_parser_ignores_command_addressed_to_other_bot() -> None:
    assert parse_command("/roast@another_bot", bot_username="pozor_me_bot") is None
    assert parse_command("/roast@pozor_me_bot", bot_username="pozor_me_bot") is not None


def test_text_mention_resolves_numeric_identity_without_username_guessing(db_path: Path) -> None:
    text = "/roast Макс"
    parsed = parse_command(text, ({"type": "text_mention", "offset": 7, "length": 3,
                                  "user": {"id": 17}},))
    assert parsed is not None
    assert parsed.numeric_targets == (17,)
    assert resolve_target(db_path, CommandContext(-1001, 18, 1, 0, text), parsed) == 17
    assert resolve_target(db_path, CommandContext(-1001, 18, 1, 0, text), parsed,
                          bot_user_ids=frozenset({17})) is None


def test_target_resolution_requires_one_observed_identity(db_path: Path) -> None:
    context = CommandContext(-1001, 18, 1, 0, "/roast @new_name")
    parsed = parse_command(context.text, ({"type": "mention", "offset": 7, "length": 9},))
    assert parsed is not None
    assert resolve_target(db_path, context, parsed, now=1770000000) == 17
    unknown_text = "/roast @unknown"
    unknown = parse_command(unknown_text, ({"type": "mention", "offset": 7, "length": 8},))
    assert unknown is not None
    assert resolve_target(db_path, context, unknown) is None


def test_ambiguous_reused_username_and_conflicting_targets_are_rejected(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute(
            """INSERT INTO members(chat_id,user_id,display_name,current_username)
               VALUES (-1001,19,'Other','new_name')"""
        )
        connection.execute(
            """INSERT INTO aliases(chat_id,user_id,username,valid_from,source)
               VALUES (-1001,19,'new_name','2026-03-01T00:00:00+00:00','live')"""
        )
    text = "/roast @new_name"
    parsed = parse_command(text, ({"type": "mention", "offset": 7, "length": 9},))
    context = CommandContext(-1001, 18, 1, 0, text)
    assert parsed is not None
    assert resolve_target(db_path, context, parsed) is None
    conflicting = CommandContext(-1001, 18, 1, 0, text, reply_user_id=17)
    assert resolve_target(db_path, conflicting, parsed) is None
    assert resolve_target(db_path, CommandContext(-1001, 18, 1, 0, text, reply_user_id=17, reply_is_bot=True), parsed) is None


def test_whois_needs_observed_member(db_path: Path) -> None:
    assert whois(db_path, -1001, 17) == "Макс (@new_name): user_id=17, chat_id=-1001"
    assert whois(db_path, -1001, 999) is None


def test_enable_is_allowlist_scoped_and_disable_closes_active(db_path: Path) -> None:
    assert set_chat_enabled(db_path, -1001, False)
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET enabled=1 WHERE chat_id=-1001")
        connection.execute(
            """INSERT INTO sessions(id,chat_id,thread_id,target_id,initiator_id,state,created_at,trigger_message_id)
               VALUES ('session_12345678',-1001,2,17,18,'active',100,1)"""
        )
    assert set_chat_enabled(db_path, -1001, False)
    assert not set_chat_enabled(db_path, 999, True)
    with connect(db_path) as connection:
        row = connection.execute("SELECT enabled FROM chats WHERE chat_id=-1001").fetchone()
        session = connection.execute("SELECT state,closed_reason FROM sessions").fetchone()
    assert row["enabled"] == 0
    assert tuple(session) == ("closed", "chat_disabled")


def test_opt_in_required_and_sessions_are_one_per_topic(db_path: Path) -> None:
    with connect(db_path) as connection:
        connection.execute("UPDATE members SET consent=0 WHERE chat_id=-1001 AND user_id=17")
    with pytest.raises(Exception, match="сначала включить"):
        start_session(db_path, chat_id=-1001, thread_id=1, target_id=17,
                      initiator_id=18, trigger_message_id=1, now=1000)
    assert set_consent(db_path, -1001, 17, True)
    session = start_session(db_path, chat_id=-1001, thread_id=1, target_id=17,
                            initiator_id=18, trigger_message_id=2, now=2000)
    assert session
    with pytest.raises(Exception, match="cooldown"):
        start_session(db_path, chat_id=-1001, thread_id=2, target_id=17,
                      initiator_id=19, trigger_message_id=3, now=2001)
    # A different target can start in another topic; scope exclusivity is per topic.
    with connect(db_path) as connection:
        connection.execute(
            "INSERT INTO members(chat_id,user_id,display_name,consent) VALUES (-1001,20,'Тест',1)"
        )
    second = start_session(db_path, chat_id=-1001, thread_id=2, target_id=20,
                           initiator_id=19, trigger_message_id=4, now=2001)
    assert second != session
    with pytest.raises(Exception, match="этой теме"):
        start_session(db_path, chat_id=-1001, thread_id=1, target_id=20,
                      initiator_id=19, trigger_message_id=5, now=2002)


def test_stop_requires_actor_and_matching_thread(db_path: Path) -> None:
    session = start_session(db_path, chat_id=-1001, thread_id=3, target_id=17,
                            initiator_id=18, trigger_message_id=10, now=1000)
    assert not close_session(db_path, session_id=session, chat_id=-1001, thread_id=3,
                             actor_id=19, now=1010)
    assert not close_session(db_path, session_id=session, chat_id=-1001, thread_id=4,
                             actor_id=17, now=1010)
    assert close_session(db_path, session_id=session, chat_id=-1001, thread_id=3,
                         actor_id=17, now=1010)
    with connect(db_path) as connection:
        row = connection.execute("SELECT state,revision FROM sessions WHERE id=?", (session,)).fetchone()
    assert row["state"] == "closed"
    assert row["revision"] == 2  # invalidates any pending generation for the old revision


def test_incoming_state_machine_limits_scope_and_coalescing_window() -> None:
    session = SessionClock("session_12345678", -1001, 2, 17, 18, 1, 0,
                           1000, 1010, 1)
    direct = IncomingEvent(-1001, 2, 17, True)
    assert decide_incoming(session, direct, 1030) == "continue"
    assert decide_incoming(session, IncomingEvent(-1001, 2, 17, False), 1030) == "ignore"
    assert decide_incoming(session, IncomingEvent(-1001, 3, 17, True), 1030) == "ignore"
    assert decide_incoming(session, IncomingEvent(-1001, 2, 19, True), 1030) == "ignore"
    assert decide_incoming(session, IncomingEvent(-1001, 2, 17, True, stop=True), 1011) == "close:explicit"
    assert decide_incoming(session, direct, 1011) == "defer"
    assert decide_incoming(SessionClock(**{**session.__dict__, "sent_turns": 4}), direct, 1030) == "close:turn_limit"


def test_expiry_and_callback_tokens(db_path: Path) -> None:
    session = start_session(db_path, chat_id=-1001, thread_id=0, target_id=17,
                            initiator_id=18, trigger_message_id=12, now=1000)
    with connect(db_path) as connection:
        connection.execute("UPDATE sessions SET last_sent_at=1010, turn_count=1 WHERE id=?", (session,))
    assert expire_sessions(db_path, now=1100) == 1
    token = callback_data("stop", session)
    assert callback_action(token) == "stop"
    assert callback_session_id(token) == session
    assert callback_session_id("stop") is None


def test_max_turn_expiry_and_natural_stop_text_is_not_a_command(db_path: Path) -> None:
    session = start_session(db_path, chat_id=-1001, thread_id=0, target_id=17,
                            initiator_id=18, trigger_message_id=13, now=1000)
    with connect(db_path) as connection:
        connection.execute("UPDATE sessions SET turn_count=4 WHERE id=?", (session,))
    assert expire_sessions(db_path, now=1001) == 1
    assert parse_command("ну всё, стоп, хватит") is None


def test_restart_closes_active_session_and_invalidates_pending_revision(db_path: Path) -> None:
    session = start_session(db_path, chat_id=-1001, thread_id=0, target_id=17,
                            initiator_id=18, trigger_message_id=14, now=1000)
    assert close_sessions_on_startup(db_path) == 1
    with connect(db_path) as connection:
        row = connection.execute("SELECT state,closed_reason,revision FROM sessions WHERE id=?", (session,)).fetchone()
    assert tuple(row) == ("closed", "restart", 2)


def test_multiple_mentions_and_reply_conflicts_are_not_guessed(db_path: Path) -> None:
    text = "/roast @new_name @caller"
    parsed = parse_command(text, (
        {"type": "mention", "offset": 7, "length": 9},
        {"type": "mention", "offset": 17, "length": 7},
    ))
    assert parsed is not None
    assert len(parsed.username_targets) == 2
    assert resolve_target(db_path, CommandContext(-1001, 18, 1, 0, text), parsed) is None


def _run_immediate(awaitable):
    try:
        awaitable.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("offline fake unexpectedly suspended")


def test_admin_commands_use_real_member_lookup_and_control_inbox(db_path: Path) -> None:
    from pozor.commands import handle_telegram_update

    class FakeBot:
        def __init__(self, is_admin: bool) -> None:
            self.is_admin = is_admin
            self.sent: list[str] = []

        async def get_chat_member(self, *, chat_id: int, user_id: int):
            class Member:
                def __init__(self, status: str) -> None:
                    self.status = status
            return Member("administrator" if self.is_admin else "member")

        async def send_message(self, *, chat_id: int, text: str, message_thread_id=None, reply_markup=None, parse_mode=None):
            self.sent.append(text)

    update = {"message": {"message_id": 40, "chat": {"id": -1001, "type": "supergroup"},
                           "from": {"id": 18, "first_name": "Инициатор"},
                           "text": "/disable_chat", "entities": [{"type": "bot_command", "offset": 0, "length": 13}]}}
    bot = FakeBot(False)
    _run_immediate(handle_telegram_update(db_path, bot, update, owner_ids=frozenset(), bot_id=99))
    assert "только владелец" in bot.sent[0]
    with connect(db_path) as connection:
        connection.execute("UPDATE chats SET enabled=1 WHERE chat_id=-1001")
    admin_bot = FakeBot(True)
    _run_immediate(handle_telegram_update(db_path, admin_bot, {"message": {**update["message"], "message_id": 41}}, owner_ids=frozenset(), bot_id=99))
    assert admin_bot.sent == ["Чат выключен; активные сессии завершены."]
    with connect(db_path) as connection:
        assert connection.execute("SELECT text FROM messages").fetchall() == []


def test_anonymous_admin_cannot_enable_and_other_actor_cannot_press_stop(db_path: Path) -> None:
    from pozor.commands import handle_telegram_update

    class FakeBot:
        calls = 0

        async def get_chat_member(self, *, chat_id: int, user_id: int):
            self.calls += 1
            return type("Member", (), {"status": "administrator"})()

        async def send_message(self, *, chat_id: int, text: str, message_thread_id=None, reply_markup=None, parse_mode=None):
            pass

        async def answer_callback_query(self, *, callback_query_id: str, text: str):
            pass

    _run_immediate(handle_telegram_update(
        db_path,
        FakeBot(),
        {"message": {"message_id": 50, "chat": {"id": -1001, "type": "supergroup"},
                     "from": {"id": 18}, "sender_chat": {"id": -1001},
                     "text": "/disable_chat", "entities": [{"type": "bot_command", "offset": 0, "length": 13}]}},
        owner_ids=frozenset(), bot_id=99,
    ))
    with connect(db_path) as connection:
        assert connection.execute("SELECT enabled FROM chats WHERE chat_id=-1001").fetchone()[0] == 1
    session = start_session(db_path, chat_id=-1001, thread_id=0, target_id=17,
                            initiator_id=18, trigger_message_id=51, now=1000)
    fake = FakeBot()
    _run_immediate(handle_telegram_update(
        db_path, fake,
        {"callback_query": {"id": "cb-1", "data": callback_data("stop", session),
                             "from": {"id": 19},
                             "message": {"message_id": 52, "chat": {"id": -1001}}}},
        owner_ids=frozenset(), bot_id=99,
    ))
    with connect(db_path) as connection:
        state = connection.execute("SELECT state FROM sessions WHERE id=?", (session,)).fetchone()[0]
    assert state == "active"


def test_roast_command_returns_session_bound_stop_buttons(db_path: Path) -> None:
    from pozor.commands import handle_telegram_update

    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def get_chat_member(self, *, chat_id: int, user_id: int):
            raise AssertionError("roast start must not make an admin lookup")

        async def send_message(self, *, chat_id: int, text: str, message_thread_id=None, reply_markup=None, parse_mode=None):
            self.sent.append({"text": text, "reply_markup": reply_markup})

    bot = FakeBot()
    update = {"message": {"message_id": 60, "chat": {"id": -1001, "type": "supergroup"},
                           "from": {"id": 18, "first_name": "Инициатор"}, "text": "/roast @new_name",
                           "entities": [{"type": "bot_command", "offset": 0, "length": 6},
                                        {"type": "mention", "offset": 7, "length": 9}]}}
    _run_immediate(handle_telegram_update(db_path, bot, update, owner_ids=frozenset(), bot_id=99))
    assert len(bot.sent) == 1
    markup = bot.sent[0]["reply_markup"]
    assert markup is not None
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    session_id = callback_session_id(callbacks[0])
    assert session_id
    assert callback_session_id(callbacks[1]) == session_id
    assert callback_action(callbacks[0]) == "stop"
    assert callback_action(callbacks[1]) == "finish"
