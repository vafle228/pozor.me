"""Locate configuration independently of the launch directory."""

import os
from pathlib import Path


def config_home() -> Path:
    if configured := os.environ.get("POZOR_HOME"):
        return Path(configured).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.home() / ".pozor"
