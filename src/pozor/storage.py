"""SQLite connection setup and checksum-verified SQL migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

MIGRATIONS_DIR: Final = Path(__file__).with_name("migrations")
_MIGRATIONS_TABLE = "schema_migrations"


class MigrationError(RuntimeError):
    """Raised when the database schema cannot be safely advanced."""


class _ClosingConnection(sqlite3.Connection):
    """Make the standard sqlite context-manager syntax release file handles."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(database_path: Path) -> sqlite3.Connection:
    """Open a configured database connection, creating its parent directory."""
    path = database_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _migration_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    versions = [path.name[:4] for path in files]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version")
    return files


def migrate(database_path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations and reject edits to migrations already applied."""
    migration_files = _migration_files(migrations_dir)
    applied_now: list[str] = []
    with connect(database_path) as connection:
        connection.execute(
            f"""CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )"""
        )
        rows = connection.execute(
            f"SELECT version, name, checksum FROM {_MIGRATIONS_TABLE}"
        ).fetchall()
        applied = {row["version"]: (row["name"], row["checksum"]) for row in rows}
        known_versions = {path.name[:4] for path in migration_files}
        unknown = set(applied) - known_versions
        if unknown:
            raise MigrationError(
                f"database contains unknown migration versions: {', '.join(sorted(unknown))}"
            )

        for path in migration_files:
            version, name = path.name[:4], path.stem[5:]
            sql = path.read_bytes()
            checksum = hashlib.sha256(sql).hexdigest()
            previous = applied.get(version)
            if previous is not None:
                if previous != (name, checksum):
                    raise MigrationError(f"applied migration {path.name} has changed")
                continue

            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql.decode("utf-8")
                    + "\n"
                    + f"INSERT INTO {_MIGRATIONS_TABLE} (version, name, checksum) "
                    + f"VALUES ({_sql_literal(version)}, {_sql_literal(name)}, {_sql_literal(checksum)});\n"
                    + "COMMIT;"
                )
            except (sqlite3.Error, UnicodeDecodeError) as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise MigrationError(f"failed to apply {path.name}: {exc}") from exc
            applied_now.append(path.name)

    return applied_now


def _sql_literal(value: str) -> str:
    """Quote an internally generated value for the migration ledger insert."""
    return "'" + value.replace("'", "''") + "'"
