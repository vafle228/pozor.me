import logging
import sqlite3
import tempfile
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from pozor.cli import app
from pozor.config import Settings
from pozor.doctor import run_checks
from pozor.logging import SecretRedactingFormatter, configure_logging


def test_settings_defaults_and_example_token_are_safe() -> None:
    settings = Settings(_env_file=None, bot_token="replace_me")

    assert settings.bot_token is None
    assert settings.database_path.as_posix() == "data/pozor.sqlite3"
    assert "replace_me" not in repr(settings)


def test_settings_reject_invalid_values_without_echoing_them() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, context_size="sensitive-value")

    assert "sensitive-value" not in str(error.value)


def test_settings_reject_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, owner_ids=[7, 7])


def test_doctor_checks_local_prerequisites_and_never_prints_credentials() -> None:
    token = "123456:private-token"
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        tmp_path = Path(temp_dir)
        settings = Settings(
            _env_file=None,
            bot_token=SecretStr(token),
            database_path=tmp_path / "nested" / "pozor.sqlite3",
        )

        checks = run_checks(settings)

        assert all(check.ok for check in checks)
        assert (tmp_path / "nested").is_dir()
        assert all(token not in check.detail for check in checks)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE VIRTUAL TABLE check_fts USING fts5(content)")


def test_logging_formatter_redacts_credentials_from_tracebacks() -> None:
    secret = "private-api-key"
    formatter = SecretRedactingFormatter("%(message)s", [secret])
    try:
        raise RuntimeError(f"request failed with {secret}")
    except RuntimeError:
        record = logging.LogRecord(
            "pozor.test", logging.ERROR, __file__, 1, "bot token=%s", (secret,),
            exc_info=__import__("sys").exc_info(),
        )

    output = formatter.format(record)

    assert secret not in output
    assert output.count("[REDACTED]") >= 2


def test_configured_logging_redacts_bot_and_llm_secrets() -> None:
    settings = Settings(
        _env_file=None,
        bot_token=SecretStr("bot-secret"),
        llm_api_key=SecretStr("llm-secret"),
    )
    configure_logging(settings)
    try:
        handler = logging.getLogger().handlers[0]
        record = logging.LogRecord(
            "pozor.test", logging.ERROR, __file__, 1, "credentials %s %s",
            ("bot-secret", "llm-secret"), None,
        )
        output = handler.format(record)
        assert "bot-secret" not in output
        assert "llm-secret" not in output
        assert output.count("[REDACTED]") == 2
    finally:
        logging.getLogger().handlers.clear()


def test_doctor_cli_missing_token_is_actionable_and_silent_about_secrets(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        monkeypatch.setenv("BOT_TOKEN", "replace_me")
        monkeypatch.setenv("DATABASE_PATH", str(Path(temp_dir) / "pozor.sqlite3"))
        result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL bot_token: BOT_TOKEN is not configured" in result.stdout
    assert "replace_me" not in result.stdout
