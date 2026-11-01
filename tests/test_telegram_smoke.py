from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat
from typer.testing import CliRunner

from pozor.cli import app
from pozor.config import Settings
from pozor.storage import connect, migrate
from pozor.telegram_smoke import TelegramSmokeError, probe


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "smoke.sqlite3"
    migrate(path)
    with connect(path) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,1,1),(-3,0,0),(-4,1,0)")
    return Settings(_env_file=None, database_path=path)


def bot():
    return SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
        get_chat=AsyncMock(return_value=SimpleNamespace(type="supergroup")),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_message=AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-1), message_id=5)),
        delete_message=AsyncMock(),
    )


async def test_live_probe_checks_all_groups_sends_only_first_and_deletes(settings):
    client = bot()
    result = await probe(client, settings, send=True)
    assert result["chats_checked"] == 2
    assert result["send_delete"] == "ok"
    client.send_message.assert_awaited_once()
    assert client.send_message.call_args.args[0] == -1
    client.delete_message.assert_awaited_once_with(-1, 5)


@pytest.mark.parametrize("damage", ["webhook", "private", "left", "empty"])
async def test_probe_refuses_unsafe_targets(damage, settings):
    client = bot()
    if damage == "webhook":
        client.get_webhook_info.return_value.url = "private-url"
    elif damage == "private":
        client.get_chat.return_value.type = "private"
    elif damage == "left":
        client.get_chat_member.return_value.status = "left"
    else:
        with connect(settings.database_path) as db:
            db.execute("UPDATE chats SET enabled=0")
    with pytest.raises(TelegramSmokeError):
        await probe(client, settings, send=True)
    client.send_message.assert_not_awaited()


def test_smoke_cli_never_echoes_api_exception(monkeypatch):
    monkeypatch.setattr("pozor.telegram_smoke.smoke", AsyncMock(
        side_effect=RuntimeError("secret-token private-chat")))
    result = CliRunner().invoke(app, ["telegram", "smoke"])
    assert result.exit_code == 1
    assert "RuntimeError" in result.output
    assert "secret-token" not in result.output
    assert "private-chat" not in result.output


async def test_inaccessible_group_has_safe_diagnostic(settings):
    client = bot()
    client.get_chat.side_effect = TelegramBadRequest(
        method=GetChat(chat_id=-1), message="private chat secret")
    with pytest.raises(TelegramSmokeError, match="^configured_chat_inaccessible$"):
        await probe(client, settings)


def test_run_never_echoes_telegram_startup_exception(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:synthetic-token")
    monkeypatch.setattr("pozor.cli.run_telegram", AsyncMock(side_effect=TelegramBadRequest(
        method=GetChat(chat_id=-1), message="private-chat synthetic-token")))
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 1
    assert "FAIL Telegram API" in result.output
    assert "private-chat" not in result.output
    assert "synthetic-token" not in result.output
