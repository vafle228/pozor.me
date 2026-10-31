"""T7 offline acceptance: WAL snapshots, recovery, safe failures and artifact identity."""

import hashlib
import json
import os
import sqlite3
import time

import pytest
from typer.testing import CliRunner

from pozor.cli import app
from pozor.config import Settings
from pozor.deployment import check_artifacts
from pozor.operations import OperationsError, backup, restore, status
from pozor.storage import connect, migrate


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'live.sqlite3'
    migrate(path)
    return path


def test_backup_includes_committed_wal_and_not_uncommitted_work(database, tmp_path):
    # Keep writer open, disabling autocheckpoint: a plain .sqlite copy would miss this.
    with connect(database) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO chats(chat_id) VALUES (-1)')
        writer.commit()
        writer.execute('INSERT INTO chats(chat_id) VALUES (-2)')
        snapshot = backup(database, tmp_path / 'backups')
        with connect(snapshot) as db:
            assert [r[0] for r in db.execute('SELECT chat_id FROM chats')] == [-1]
    target = tmp_path / 'restored.sqlite3'
    restore(snapshot, target)
    assert status(target)['schema'] == 'current'
    assert migrate(target) == []


@pytest.mark.parametrize('state,expected', [('ready', 'cancelled'), ('sending', 'unknown'),
                                           ('sent', 'sent')])
def test_restore_cancels_pending_and_preserves_sent(database, tmp_path, state, expected):
    with connect(database) as db:
        db.execute('INSERT INTO chats(chat_id) VALUES (-1)')
        db.execute("INSERT INTO sessions(id,chat_id,target_id,initiator_id,state,created_at,"
                   "trigger_message_id) VALUES ('synthetic',-1,42,43,'active',1000,1)")
        db.execute('INSERT INTO turns(session_id,turn_no,revision,status) VALUES (?,?,?,?)',
                   ('synthetic', 1, 1, state))
        db.execute('INSERT INTO outbox(session_id,turn_no,status,next_attempt_at) VALUES (?,?,?,0)',
                   ('synthetic', 1, state))
        db.execute('INSERT INTO poller_state(bot_id,next_offset) VALUES (99,123)')
    snapshot = backup(database, tmp_path / 'backups')
    restored = tmp_path / 'restored.sqlite3'
    restore(snapshot, restored)
    with connect(restored) as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == expected
        assert db.execute('SELECT status FROM turns').fetchone()[0] == expected
        assert db.execute('SELECT closed_reason FROM sessions').fetchone()[0] == 'restart'
        assert db.execute('SELECT next_offset FROM poller_state').fetchone()[0] == 123
    # Recovery affects only the restored timeline.
    with connect(snapshot) as db:
        assert db.execute('SELECT state FROM sessions').fetchone()[0] == 'active'


@pytest.mark.parametrize('damage', ['schema', 'foreign_key', 'fts', 'checksum'])
def test_invalid_snapshot_is_never_published(database, tmp_path, damage):
    with connect(database) as db:
        if damage == 'schema':
            db.execute("INSERT INTO schema_migrations VALUES ('9999','future','x','now')")
        elif damage == 'checksum':
            db.execute("UPDATE schema_migrations SET checksum='bad' WHERE version='0001'")
        elif damage == 'foreign_key':
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT INTO members(chat_id,user_id) VALUES (-999,42)')
        else:
            db.execute("INSERT INTO messages_fts(rowid,text) VALUES (999,'orphan synthetic')")
    directory = tmp_path / 'backups'
    with pytest.raises((OperationsError, sqlite3.Error)):
        backup(database, directory)
    assert list(directory.iterdir()) == []


def test_restore_refuses_existing_path_and_sidecars(database, tmp_path):
    before = database.read_bytes()
    with pytest.raises(OperationsError):
        restore(database, database)
    assert database.read_bytes() == before
    destination = tmp_path / 'new.sqlite3'
    sidecar = tmp_path / 'new.sqlite3-wal'
    sidecar.write_bytes(b'synthetic sentinel')
    with pytest.raises(OperationsError):
        restore(database, destination)
    assert not destination.exists()
    assert sidecar.read_bytes() == b'synthetic sentinel'


def test_rotation_keeps_unrelated_files_and_only_runs_after_success(database, tmp_path):
    directory = tmp_path / 'backups'
    old = backup(database, directory)
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    unrelated = directory / 'operator.sqlite3'
    unrelated.write_bytes(b'keep')
    with connect(database) as db:
        db.execute("UPDATE schema_migrations SET checksum='bad'")
    with pytest.raises(OperationsError):
        backup(database, directory)
    assert old.exists()
    # Rebuild a compatible empty database; successful rotation removes only owned snapshots.
    other = tmp_path / 'other.sqlite3'
    migrate(other)
    newest = backup(other, directory)
    assert newest.exists() and not old.exists() and unrelated.exists()


def test_status_and_failures_are_offline_and_do_not_echo_data(database, monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_PATH', str(database))
    monkeypatch.setenv('BOT_TOKEN', '123456:synthetic-secret')
    with connect(database) as db:
        db.execute("INSERT INTO inbox(update_id,normalized_payload) VALUES (1,'private message')")
    runner = CliRunner()
    result = runner.invoke(app, ['status'])
    assert result.exit_code == 0
    assert json.loads(result.stdout)['inbox_pending'] == 1
    assert 'private message' not in result.stdout and 'synthetic-secret' not in result.stdout
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'missing.sqlite3'))
    result = runner.invoke(app, ['status'])
    assert result.exit_code == 1
    assert not (tmp_path / 'missing.sqlite3').exists()
    assert 'synthetic-secret' not in result.output
    result = runner.invoke(app, ['db', 'backup', '--directory', str(tmp_path / 'snapshots')])
    assert result.exit_code == 1 and 'Traceback' not in result.output


def test_cli_backup_restore(database, monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_PATH', str(database))
    runner = CliRunner()
    directory = tmp_path / 'snapshots'
    assert runner.invoke(app, ['db', 'backup', '--directory', str(directory)]).exit_code == 0
    source = next(directory.glob('*.sqlite3'))
    target = tmp_path / 'recovery.sqlite3'
    args = ['db', 'restore', '--source', str(source), '--destination', str(target)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args).exit_code == 1


def test_deployment_checks_file_hashes_configuration_and_missing_baseline(tmp_path):
    model, server = tmp_path / 'synthetic.gguf', tmp_path / 'synthetic.exe'
    model.write_bytes(b'synthetic model, no weights')
    server.write_bytes(b'synthetic server, never executed')

    def artifact(path):
        return {'filename': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({
        'schema_version': 1, 'llama_cpp_build': 'synthetic-build', 'llama_cpp_artifact': artifact(server),
        'model': artifact(model) | {'repo': 'synthetic/repo', 'revision': 'a' * 40, 'alias': 'pozor-local'},
        'runtime': {'context_size': 8192, 'slots': 1, 'enable_thinking': False, 'max_output_tokens': 180},
    }))
    settings = Settings(_env_file=None)
    check_artifacts(lock, model, server, settings)
    with pytest.raises(OperationsError):
        check_artifacts(lock, model, server, Settings(_env_file=None, context_size=4096))
    model.write_bytes(b'wrong content')
    with pytest.raises(OperationsError):
        check_artifacts(lock, model, server, settings)
    result = CliRunner().invoke(app, ['deployment', 'check', '--model', str(model),
                                     '--server', str(server)])
    assert result.exit_code == 1 and 'incomplete lock' in result.output
