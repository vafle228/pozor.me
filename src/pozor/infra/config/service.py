"""Validated runtime overrides with provenance and an append-only audit trail."""

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import SecretStr

from pozor.infra.config.paths import config_home
from pozor.infra.config.registry import Settings, owner, registry
from pozor.infra.sqlite.settings_repo import SettingsRepository


class FrozenSettings(Settings):
    model_config = Settings.model_config | {'frozen':True}


class SettingsService:
    def __init__(self, seed: Settings, cli: dict[str, Any] | None = None):
        self.seed = seed.model_copy(deep=True)
        self.cli = cli or {}
        if any(name not in Settings.model_fields or owner(name)!='runtime' for name in self.cli):
            raise ValueError('CLI overrides must name registered runtime settings')
        self.repository = SettingsRepository(seed.database_path)

    def snapshot(self) -> Settings:
        values = self.seed.model_dump()
        for name,value in self.repository.overrides().items():
            if name in Settings.model_fields and owner(name)=='runtime':
                values[name]=json.loads(value)
        values.update(self.cli)
        # model_validate bypasses environment loading; the snapshot is immutable.
        result = FrozenSettings.model_validate(values)
        result.__pydantic_fields_set__.update(values)
        return result

    def set(self, name: str, value: Any, *, actor: str = "cli") -> None:
        if name not in Settings.model_fields or owner(name) != "runtime":
            raise ValueError("Only registered runtime settings can be changed here")
        values = self.snapshot().model_dump() | {name: value}
        validated = Settings.model_validate(values)
        value_json = json.dumps(validated.model_dump(mode='json')[name], ensure_ascii=False)
        self.repository.save(name,value_json,actor)
        if name in {'live_retention_days','import_retention_days'}:
            self.repository.retention(validated.live_retention_days,validated.import_retention_days)

    def reset(self, name: str, *, actor: str = "cli") -> None:
        if name not in Settings.model_fields or owner(name) != 'runtime':
            raise ValueError("Only runtime settings can be reset")
        self.repository.save(name,None,actor)
        if name in {'live_retention_days','import_retention_days'}:
            snapshot=self.snapshot()
            self.repository.retention(snapshot.live_retention_days,snapshot.import_retention_days)

    def admin_password(self) -> str | None:
        # Environment ownership is unchanged. Re-read the owned dotenv file so
        # password rotation takes effect without restarting the HTTP server.
        fresh=Settings() if (config_home()/'.env').is_file() or 'ADMIN_PASSWORD' in os.environ else self.seed
        return fresh.admin_password.get_secret_value() if fresh.admin_password else None

    def rollback(self, revision: int) -> None:
        item=next((r for r in self.repository.history() if r['id']==revision),None)
        if item is None:
            raise ValueError('unknown settings revision')
        if item['previous_json'] is None:
            self.reset(item['name'])
        else:
            self.set(item['name'],json.loads(item['previous_json']))

    def describe(self) -> list[dict[str, Any]]:
        snapshot = self.snapshot()
        dotenv = dotenv_values(config_home() / '.env')
        overridden = set(self.repository.overrides())
        result = []
        for item in registry():
            name = item['name']
            value = getattr(snapshot, name)
            source = ('cli' if name in self.cli else 'override' if name in overridden else
                      'env' if name.upper() in os.environ or name.upper() in dotenv else
                      'toml' if (config_home() / 'pozor.toml').exists() and name in toml_keys() else 'default')
            credentials=self.seed.database_path.parent/'secrets.local.json'
            if owner(name)=='credentials' and credentials.is_file():
                source='admin'
                value=True
            if isinstance(value, SecretStr) or owner(name) in {'secret', 'credentials'}:
                value = bool(value)
            elif isinstance(value, Path):
                value = str(value)
            result.append(item | {'value': value, 'source': source})
        return result


def toml_keys() -> set[str]:
    import tomllib

    with (config_home() / 'pozor.toml').open('rb') as stream:
        return set(tomllib.load(stream))


def conflicts(database: Path) -> list[str]:
    import sqlite3
    dotenv=dotenv_values(config_home()/'.env')
    toml=toml_keys() if (config_home()/'pozor.toml').is_file() else set()
    try:
        overrides=SettingsRepository(database).overrides() if database.is_file() else {}
    except sqlite3.Error:
        overrides={}
    return [name for name in Settings.model_fields if sum((name.upper() in os.environ,
        name.upper() in dotenv,name in toml,name in overrides))>1]


def template() -> str:
    lines = ['# Generated by pozor config template. Runtime settings: pozor config set / admin.',
             '# Configuration directory: POZOR_HOME, otherwise the project root.',
             '# POZOR_HOME=/path/to/pozor']
    for item in registry():
        name = item['name']
        if owner(name) == 'runtime':
            continue
        value = Settings.model_fields[name].get_default(call_default_factory=True)
        if isinstance(value,Path):
            value = value.as_posix()
        if value is None:
            value = ''
        if isinstance(value, (list, bool)):
            value = json.dumps(value)
        lines += [f"# {item['description']}; owner={item['owner']}; {'restart required' if item['restart_required'] else 'dotenv rotation without restart'}.",
                  f'{name.upper()}={value}']
    return '\n'.join(lines) + '\n'
