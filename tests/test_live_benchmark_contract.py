import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from pozor.app.container import DiagnosticBusy, standalone_inference
from pozor.app.humor_eval import compare
from pozor.cli import app
from pozor.config import Settings
from pozor.core.domain.generation import InferenceError
from pozor.core.usecases.generate import FALLBACK
from pozor.evaluation import benchmark
from pozor.infra.files.process_lock import ProcessLock


async def test_benchmark_uses_configured_deadline_and_fails_model_fallback(monkeypatch):
    monkeypatch.setenv("GENERATION_DEADLINE_SECONDS", "37")
    budgets = []
    calls = []

    class Client:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def check(self, deadline):
            budgets.append(deadline - asyncio.get_running_loop().time())

        async def wait_idle(self, deadline):
            calls.append('wait_idle')

    class Generator:
        def __init__(self, client):
            pass

        async def generate(self, pack, deadline):
            budgets.append(deadline - asyncio.get_running_loop().time())
            calls.append('generate')
            return SimpleNamespace(fallback=True, roast=SimpleNamespace(
                text="fallback", evidence_ids=[]))

    monkeypatch.setattr("pozor.app.container.LlamaClient", Client)
    monkeypatch.setattr("pozor.evaluation.Generator", Generator)
    report = await benchmark(Path("tests/fixtures/eval.jsonl"), live=True)
    assert report["retrieval_pass"]
    assert report["model_pass"] is False
    assert budgets and all(36 < value <= 37 for value in budgets)
    generated = sum(len(row['generations']) for row in report['results'])
    assert calls == ['wait_idle', 'generate'] * (3 + generated)
    assert report['model_latency_ms']['p50'] >= 0 and report['queue_wait_ms']['p50'] >= 0


async def test_diagnostic_ownership_cancellation_and_database_isolation(tmp_path):
    settings = Settings(database_path=tmp_path / 'uninitialized.sqlite3')
    started = asyncio.Event()
    finished = asyncio.Event()
    resources = []

    async def operation(deadline):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def diagnostic():
        async with standalone_inference(settings) as (client, queue):
            resources.append((client, queue))
            queue.wait_idle = AsyncMock()
            await queue.submit(key="cancel", chat_id=-1, factory=operation,
                               deadline=asyncio.get_running_loop().time() + 5)

    owner = asyncio.create_task(diagnostic())
    await asyncio.wait_for(started.wait(), 2)
    try:
        with pytest.raises(DiagnosticBusy):
            async with standalone_inference(settings):
                pytest.fail("Two diagnostics acquired the same installation")
        with pytest.raises(ValueError):
            ProcessLock(settings.database_path).acquire()
    finally:
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    assert finished.is_set()
    client, queue = resources[0]
    assert queue.closed and queue.worker.done() and client.http.is_closed
    async with standalone_inference(settings) as (client, queue):
        assert not queue.closed
    assert queue.closed and client.http.is_closed
    assert not settings.database_path.exists()


async def test_humor_comparison_queues_each_variant_with_its_settings(monkeypatch, tmp_path):
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(json.dumps({'id': 'one', 'trigger': 'Придумал пароль'}) + '\n', 'utf-8')
    output = tmp_path / 'pairs.json'
    settings = Settings(database_path=tmp_path / 'unused.sqlite3')
    idle = AsyncMock()
    variants = []

    async def generate(generator, pack, deadline):
        variants.append(generator.client.settings.humor_v2_enabled)
        assert idle.await_count == len(variants)
        return SimpleNamespace(
            roast=SimpleNamespace(text="Реплика", angle="fallback"), fallback=True, latency_ms=1
        )

    monkeypatch.setattr("pozor.infra.llm.client.LlamaClient.wait_idle", idle)
    monkeypatch.setattr("pozor.app.humor_eval.Generator.generate", generate)
    report = await compare(dataset, ['B', 'A'], output, settings)
    assert variants == [True, False] and report['pairs'] == 1
    pair, = json.loads(output.read_text('utf-8'))['pairs']
    assert {pair['left']['variant'], pair['right']['variant']} == {'A', 'B'}
    assert pair['left']['fallback'] and pair['right']['fallback'] and pair['vote'] is None
    assert not settings.database_path.exists()


async def test_humor_comparison_keeps_fallback_when_slot_check_fails(monkeypatch, tmp_path):
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(json.dumps({'id': 'one', 'trigger': 'Придумал пароль'}) + '\n', 'utf-8')
    settings = Settings(database_path=tmp_path / 'unused.sqlite3')
    output = tmp_path / 'pairs.json'
    idle = AsyncMock(side_effect=InferenceError('expected_one_slot'))
    generate = AsyncMock(side_effect=AssertionError("Generator ran without an idle slot"))
    monkeypatch.setattr("pozor.infra.llm.client.LlamaClient.wait_idle", idle)
    monkeypatch.setattr("pozor.app.humor_eval.Generator.generate", generate)
    await compare(dataset, ['A', 'B'], output, settings)
    pair, = json.loads(output.read_text('utf-8'))['pairs']
    answers = {answer['variant']: answer for answer in (pair['left'], pair['right'])}
    assert idle.await_count == 2 and not generate.await_count
    assert all(answer['fallback'] for answer in answers.values())
    assert answers['A']['text'] == FALLBACK
    assert answers['B']['text'].startswith("Участник")


def test_cli_model_failure_returns_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr("pozor.evaluation.benchmark", AsyncMock(return_value={
        "retrieval_pass": True, "model_pass": False}))
    result = CliRunner().invoke(app, ["benchmark", "--live", "--suite",
        "tests/fixtures/eval.jsonl", "--output", str(tmp_path / "report.json")])
    assert result.exit_code == 1


def test_model_commands_report_busy_and_offline_check_stays_available(monkeypatch, tmp_path):
    database = tmp_path / 'owned.sqlite3'
    monkeypatch.setenv('DATABASE_PATH', str(database))
    report = tmp_path / 'live-report.json'
    lock = ProcessLock(database)
    lock.acquire()
    try:
        runner = CliRunner()
        for command in (
            ['llm', 'smoke'],
            ['eval', 'humor', '--output', str(report)],
            ['benchmark', '--live', '--suite', 'tests/fixtures/eval.jsonl', '--output', str(report)],
        ):
            result = runner.invoke(app, command)
            assert result.exit_code == 1, result.output
            assert 'занята' in result.output
        assert runner.invoke(app, ['eval', 'humor', '--check']).exit_code == 0
    finally:
        lock.release()
    assert not database.exists() and not report.exists()
