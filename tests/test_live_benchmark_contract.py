import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from typer.testing import CliRunner

from pozor.cli import app
from pozor.evaluation import benchmark


async def test_benchmark_uses_configured_deadline_and_fails_model_fallback(monkeypatch):
    monkeypatch.setenv("GENERATION_DEADLINE_SECONDS", "37")
    budgets = []

    class Client:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def check(self, deadline):
            budgets.append(deadline - asyncio.get_running_loop().time())

    class Generator:
        def __init__(self, client):
            pass

        async def generate(self, pack, deadline):
            budgets.append(deadline - asyncio.get_running_loop().time())
            return SimpleNamespace(fallback=True, roast=SimpleNamespace(
                text="fallback", evidence_ids=[]))

    monkeypatch.setattr("pozor.evaluation.LlamaClient", Client)
    monkeypatch.setattr("pozor.evaluation.Generator", Generator)
    report = await benchmark(Path("tests/fixtures/eval.jsonl"), live=True)
    assert report["retrieval_pass"]
    assert report["model_pass"] is False
    assert budgets and all(36 < value <= 37 for value in budgets)


def test_cli_model_failure_returns_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr("pozor.evaluation.benchmark", AsyncMock(return_value={
        "retrieval_pass": True, "model_pass": False}))
    result = CliRunner().invoke(app, ["benchmark", "--live", "--suite",
        "tests/fixtures/eval.jsonl", "--output", str(tmp_path / "report.json")])
    assert result.exit_code == 1
