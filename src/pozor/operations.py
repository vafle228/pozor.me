"""Offline operations. Snapshots use SQLite backup, never a live file copy."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pozor.storage import MIGRATIONS_DIR


class OperationsError(RuntimeError):
    """An operation refused unsafe or incompatible input."""


@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    # mode=ro fails on missing files and preserves live WAL visibility.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        yield db
    finally:
        db.close()


def _schema(db: sqlite3.Connection) -> None:
    expected = [
        (p.name[:4], p.stem[5:], hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9][0-9]_*.sql'))
    ]
    actual = db.execute(
        'SELECT version,name,checksum FROM schema_migrations ORDER BY version'
    ).fetchall()
    if actual != expected:
        raise OperationsError('schema mismatch; use the matching application version or migrate first')


def _validate(db: sqlite3.Connection) -> None:
    _schema(db)
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise OperationsError('SQLite integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise OperationsError('foreign key check failed')
    # rank=1 also compares the external-content FTS index with messages.
    db.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")
    db.commit()


def _snapshot(source: Path, destination: Path, *, recover: bool) -> None:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination.exists():
        raise OperationsError('destination must be a new separate path')
    if any(Path(str(destination) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise OperationsError('destination has SQLite sidecars; choose a new path')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pozor-snapshot-', dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with read_database(source) as origin, closing(sqlite3.connect(temporary)) as snapshot:
            _schema(origin)
            # Bound a backup under sustained write contention instead of hanging forever.
            deadline = time.monotonic() + 30

            def progress(status: int, remaining: int, total: int) -> None:
                if time.monotonic() >= deadline:
                    raise OperationsError('backup deadline exceeded; retry during lower write load')

            origin.backup(snapshot, pages=256, progress=progress, sleep=.05)
            snapshot.execute('PRAGMA journal_mode=DELETE')
            _validate(snapshot)
        if recover:
            # On a restored timeline no saved pending punch may be retried.
            from pozor.delivery import recover_delivery

            recover_delivery(temporary)
            with closing(sqlite3.connect(temporary)) as snapshot:
                snapshot.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                snapshot.execute('PRAGMA journal_mode=DELETE')
                _validate(snapshot)
        # Windows rename refuses an existing destination; POSIX rename would overwrite.
        # Both paths publish a complete file without replacing an existing database.
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def backup(database: Path, directory: Path, *, retention_days: int = 7) -> Path:
    """Create a validated snapshot; rotate only owned snapshots after success.

    Directory ACLs are the operator's responsibility, especially on Windows.
    """
    if retention_days < 1:
        raise OperationsError('retention must be at least one day')
    directory = directory.resolve()
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    destination = directory / f'pozor-backup-{stamp}-{uuid.uuid4().hex}.sqlite3'
    _snapshot(database, destination, recover=False)
    cutoff = time.time() - retention_days * 86400
    for path in directory.glob('pozor-backup-????????T??????Z-????????????????????????????????.sqlite3'):
        if (path not in (destination, database.resolve()) and not path.is_symlink()
                and path.stat().st_mtime < cutoff):
            path.unlink()
    return destination


def restore(source: Path, destination: Path) -> None:
    """Restore to a NEW offline path, validate and cancel pre-snapshot delivery."""
    _snapshot(source, destination, recover=True)


def status(database: Path) -> dict[str, object]:
    """Counts only; no messages, identities, credentials or prompts."""
    with read_database(database) as db:
        db.execute('BEGIN')
        _schema(db)
        return {
            'schema': 'current',
            'inbox_pending': db.execute(
                'SELECT count(*) FROM inbox WHERE processed_at IS NULL'
            ).fetchone()[0],
            'sessions_active': db.execute(
                "SELECT count(*) FROM sessions WHERE state='active'"
            ).fetchone()[0],
            'outbox': dict(db.execute('SELECT status,count(*) FROM outbox GROUP BY status')),
            'messages': db.execute('SELECT count(*) FROM messages').fetchone()[0],
            'database_bytes': db.execute('PRAGMA page_count').fetchone()[0]
            * db.execute('PRAGMA page_size').fetchone()[0],
        }
