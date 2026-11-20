import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pozor.app.conversation_replay import replay
from pozor.presentation.cli.main import app


async def test_offline_replay_characterizes_legacy_delivery(tmp_path, monkeypatch):
    production = tmp_path / "must-not-open.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(production))
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid")

    async def unexpected_http(*args, **kwargs):
        pytest.fail("offline replay attempted HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_http)
    report = await replay(Path("tests/fixtures/conversation_replay.json"), live=False, port=18080)
    assert not production.exists()
    assert report["mode"] == "fixture_generator"
    assert report["human_quality"] == "not_evaluated"
    scenes = {scene["scenario"]: scene for scene in report["scenarios"]}

    group = scenes["overlapping_requests"]
    assert [call["reply_to"] for call in group["attempts"]] == [6]
    assert [g["target"] for g in group["generations"]] == [13, 11]
    assert group["missed_replies"] == [5, 7]
    assert group["unwanted_interventions"] == []
    assert group["inputs_without_parent"] == []

    topics = scenes["forum_topics"]
    assert [(a["reply_to"], a["thread"]) for a in topics["attempts"]] == [(3, 101), (4, 202)]
    assert topics["generations"][0]["recent"] == [{"id": 1, "author": 11, "thread": 101}]
    for g in topics["generations"]:
        assert all(e["thread"] == g["thread"] for e in g["recent"])

    for name in ("edited_before_send", "consent_revoked_before_send", "restart_before_send"):
        assert scenes[name]["generations"]
        assert scenes[name]["attempts"] == []
        assert [turn["status"] for turn in scenes[name]["turns"]] == ["cancelled"]
    forgotten = scenes["forgotten_before_send"]
    assert forgotten["generations"] and not forgotten["attempts"] and not forgotten["turns"]
    unknown = scenes["ambiguous_send_and_restart"]
    assert [a["outcome"] for a in unknown["attempts"]] == ["unknown"]
    assert unknown["turns"][0]["outbox"] == "unknown"
    assert unknown["turns"][0]["error_code"] == "ambiguous_http"


def test_cli_writes_new_report_and_preserves_existing_file(tmp_path):
    scene = [
        {
            "name": "quiet",
            "events": [
                {"kind": "message", "at": 0, "id": 1, "author": 11, "text": "Ставлю чайник."},
                {
                    "kind": "message",
                    "at": 1,
                    "id": 2,
                    "author": 12,
                    "reply_to_bot_for": 1,
                    "text": "Уже готово?",
                    "expect": "reply",
                },
            ],
        }
    ]
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(scene), encoding="utf-8")
    output = tmp_path / "reports" / "result.json"
    args = ["eval", "replay", "--suite", str(suite), "--output", str(output)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    saved = output.read_bytes()
    scene = json.loads(saved)["scenarios"][0]
    assert scene["inputs_without_parent"] == [2]
    assert scene["attempts"] == [] and scene["missed_replies"] == []
    assert CliRunner().invoke(app, args).exit_code != 0
    assert output.read_bytes() == saved
