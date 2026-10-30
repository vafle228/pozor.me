"""Offline checks for local configuration and SQLite capabilities."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pozor.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = [Check("config", True, "settings are valid")]

    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE doctor_fts USING fts5(content)")
        checks.append(Check("sqlite_fts5", True, sqlite3.sqlite_version))
    except sqlite3.Error:
        checks.append(Check("sqlite_fts5", False, "SQLite was built without FTS5"))

    directory = Path(settings.database_path).expanduser().resolve().parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".pozor-write-check"
        probe.write_bytes(b"")
        probe.unlink()
        checks.append(Check("data_directory", True, str(directory)))
    except OSError as exc:
        checks.append(Check("data_directory", False, f"{directory}: {exc.strerror or type(exc).__name__}"))

    if settings.bot_token is None:
        checks.append(Check("bot_token", False, "BOT_TOKEN is not configured"))
    else:
        checks.append(Check("bot_token", True, "configured"))
    return checks
