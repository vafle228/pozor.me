"""Offline tests must never load the operator's real dotenv configuration."""

import pytest

from pozor.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv('POZOR_HOME', str(tmp_path))
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
