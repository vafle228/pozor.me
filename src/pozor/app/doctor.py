"""Offline checks for local configuration and SQLite capabilities."""

import tempfile
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

    from pozor.infra.sqlite.health_repo import fts5_available
    available,detail=fts5_available()
    checks.append(Check('sqlite_fts5',available,detail))

    directory = Path(settings.database_path).expanduser().resolve().parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory, prefix=".pozor-write-check-") as probe:
            probe.write(b"probe")
            probe.flush()
        checks.append(Check("data_directory", True, str(directory)))
    except OSError as exc:
        checks.append(Check("data_directory", False, f"{directory}: {exc.strerror or type(exc).__name__}"))

    if settings.bot_token is None:
        checks.append(Check("bot_token", False, "BOT_TOKEN is not configured"))
    else:
        checks.append(Check("bot_token", True, "configured"))
    if settings.admin_password is None:
        checks.append(Check('admin_password',False,'ADMIN_PASSWORD is not configured; serve/admin require at least 16 characters'))
    else:
        checks.append(Check('admin_password',len(settings.admin_password.get_secret_value())>=16,
                            'configured' if len(settings.admin_password.get_secret_value())>=16 else 'minimum 16 characters required'))
    from pozor.infra.config.service import conflicts
    for key in conflicts(settings.database_path):
        checks.append(Check('config_sources',True,f'warning: {key} is configured in multiple sources; see config show'))
    return checks
