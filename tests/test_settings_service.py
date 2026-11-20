import json

import pytest

from pozor.config import Settings
from pozor.infra.config.service import SettingsService
from pozor.storage import connect, migrate
from pozor.telegram_account import TelegramAccount


def test_admin_credentials_win_over_environment_and_legacy_migrates(tmp_path):
    directory = tmp_path / 'telegram'
    directory.mkdir()
    legacy = directory / 'application.json'
    legacy.write_text(json.dumps({'api_id': 999, 'api_hash': 'b'*32}), 'utf-8')
    account = TelegramAccount(Settings(_env_file=None, telegram_api_id=123, telegram_api_hash='a'*32), directory)
    assert account.credentials() == (999, 'b'*32)
    assert account.credentials_source == 'admin'
    assert not legacy.exists() and (tmp_path/'secrets.local.json').exists()


def test_runtime_override_is_validated_audited_and_visible_without_restart(tmp_path):
    db = tmp_path/'settings.sqlite3'
    migrate(db)
    service = SettingsService(Settings(_env_file=None, database_path=db, temperature=.8))
    before = service.snapshot()
    service.set('temperature', 1.1, actor='test')
    assert before.temperature == .8 and service.snapshot().temperature == 1.1
    assert next(i for i in service.describe() if i['name']=='temperature')['source'] == 'override'
    with pytest.raises(ValueError):
        service.set('temperature', 99)
    with pytest.raises(ValueError):
        service.set('llm_base_url', 'http://other')
    with connect(db) as connection:
        assert connection.execute('SELECT count(*) FROM settings_audit').fetchone()[0] == 1
    service.reset('temperature')
    assert service.snapshot().temperature == .8


def test_env_file_is_resolved_from_home_independently_of_current_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('POZOR_HOME', str(tmp_path))
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    (tmp_path/'.env').write_text('ADMIN_PASSWORD=synthetic-admin-password\n', 'utf-8')
    assert Settings().admin_password.get_secret_value() == 'synthetic-admin-password'
