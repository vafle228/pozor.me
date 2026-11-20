import hashlib
import json

import pytest
from typer.testing import CliRunner

from ops.humor_blind import app


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def prepare(tmp_path, count):
    dialogs = tmp_path / "dialogs.jsonl"
    rows = [
        {
            "id": str(i),
            "context": [{"author": "А", "text": "Вопрос"}, {"author": "Б", "text": str(i)}],
        }
        for i in range(count)
    ]
    dialogs.write_bytes("\n".join(json.dumps(row) for row in rows).encode())
    series = []
    for name in ("reference", "candidate"):
        path = tmp_path / f"{name}.json"
        write_json(
            path,
            {
                "variant": f"private-{name}",
                "suite_sha256": hashlib.sha256(dialogs.read_bytes()).hexdigest(),
                "responses": {row["id"]: f"{name} {row['id']}" for row in rows},
            },
        )
        series.append(path)
    return [str(p) for p in [dialogs, *series, tmp_path / "run"]]


def test_blind_review_resumes_without_key_and_locks_votes_to_booklet(tmp_path):
    args = prepare(tmp_path, 2)
    runner = CliRunner()
    assert runner.invoke(app, ["pack", *args]).exit_code == 0
    assert runner.invoke(app, ["pack", *args]).exit_code != 0
    run = tmp_path / "run"
    key = run / "key" / "key.json"
    original = key.read_bytes()
    key.unlink()
    review = run / "review"
    booklet = (review / "booklet.json").read_bytes()
    assert b"private-" not in booklet
    assert all(set(pair) == {"context", "A", "B"} for pair in json.loads(booklet))
    result = runner.invoke(app, ["review", str(review)], input="да\n2\n1\nA\n")
    # Ввод закончился на второй паре. Первая уже сохранена.
    assert result.exit_code != 0
    assert len(list(review.glob("vote-*.json"))) == 1
    key.write_bytes(original)
    interim = runner.invoke(app, ["score", str(run)])
    assert "1 из 2" in interim.output and "private-" not in interim.output
    result = runner.invoke(app, ["review", str(review)], input="нет\n0\n0\n=\n")
    assert result.exit_code == 0
    assert "Разговор 1/2" not in result.output
    values = json.loads(runner.invoke(app, ["score", str(run)]).output)
    assert values["ties"] == 1 and not values["accepted"]
    assert values["should_join"] == {"да": 1, "нет": 1}
    (review / "booklet.json").write_bytes(booklet + b"\n")
    assert runner.invoke(app, ["score", str(run)]).exit_code != 0
    assert runner.invoke(app, ["review", str(review)]).exit_code != 0


@pytest.mark.parametrize(("wins", "accepted"), [(23, False), (24, True)])
def test_full_ballot_counts_draws_separately(tmp_path, wins, accepted):
    args = prepare(tmp_path, 40)
    runner = CliRunner()
    assert runner.invoke(app, ["pack", *args]).exit_code == 0
    run = tmp_path / "run"
    key = json.loads((run / "key" / "key.json").read_bytes())
    sides = key["candidate_side"]
    assert sides.count("A") == sides.count("B") == 20
    questions = []
    for i, side in enumerate(sides):
        choice = side if i < wins else "="
        # Оба текста действительно относятся к текущему диалогу.
        questions.extend(["?", "1", "0", choice])
    pairs = json.loads((run / "review" / "booklet.json").read_bytes())
    for pair, side, case in zip(pairs, sides, key["cases"], strict=True):
        assert pair[side] == f"candidate {case}"
        assert pair["context"][-1]["text"] == case
    assert (
        runner.invoke(
            app, ["review", str(run / "review")], input="\n".join(questions) + "\n"
        ).exit_code
        == 0
    )
    score = json.loads(runner.invoke(app, ["score", str(run)]).output)
    assert (score["accepted"], score["wins"], score["ties"]) == (accepted, wins, 40 - wins)
    assert score["humor"] == {"candidate": {"0": 20, "1": 20}, "reference": {"0": 20, "1": 20}}


@pytest.mark.parametrize("fault", ["missing", "wrong_suite", "same_variant"])
def test_pack_rejects_mismatched_series_before_writing(tmp_path, fault):
    args = prepare(tmp_path, 2)
    path = tmp_path / "candidate.json"
    result = json.loads(path.read_bytes())
    if fault == "missing":
        del result["responses"]["0"]
    elif fault == "wrong_suite":
        result["suite_sha256"] = "not-this-suite"
    else:
        result["variant"] = "private-reference"
    write_json(path, result)
    assert CliRunner().invoke(app, ["pack", *args]).exit_code != 0
    assert not (tmp_path / "run").exists()


def test_duplicate_conversation_with_distinct_ids_is_rejected(tmp_path):
    args = prepare(tmp_path, 2)
    suite = tmp_path / "dialogs.jsonl"
    rows = [json.loads(line) for line in suite.read_bytes().splitlines()]
    rows[1]["context"] = rows[0]["context"]
    suite.write_bytes(b"\n".join(json.dumps(row).encode() for row in rows))
    for name in ("reference", "candidate"):
        path = tmp_path / f"{name}.json"
        series = json.loads(path.read_bytes())
        series["suite_sha256"] = hashlib.sha256(suite.read_bytes()).hexdigest()
        write_json(path, series)
    runner = CliRunner()
    result = runner.invoke(app, ["pack", *args])
    assert result.exit_code != 0 and "уникальный контекст" in result.output
    run = tmp_path / "run"
    assert not run.exists()

    # Бланк, созданный прежней версией с повтором, также не даёт итоговой оценки.
    review = run / "review"
    review.mkdir(parents=True)
    write_json(
        review / "booklet.json", [{"context": r["context"], "A": "а", "B": "б"} for r in rows]
    )
    for command, directory in (("review", review), ("score", run)):
        result = runner.invoke(app, [command, str(directory)])
        assert result.exit_code != 0 and "уникальный контекст" in result.output


def test_identical_answers_are_draws_even_with_old_side_votes(tmp_path):
    args = prepare(tmp_path, 40)
    path = tmp_path / "candidate.json"
    candidate = json.loads(path.read_bytes())
    candidate["responses"] = json.loads((tmp_path / "reference.json").read_bytes())["responses"]
    write_json(path, candidate)
    runner = CliRunner()
    assert runner.invoke(app, ["pack", *args]).exit_code == 0
    run = tmp_path / "run"
    review = run / "review"
    result = runner.invoke(app, ["review", str(review)], input="нет\n1\n1\n" * 40)
    assert result.exit_code == 0 and "Какой ответ лучше" not in result.output
    sides = json.loads((run / "key" / "key.json").read_bytes())["candidate_side"]
    for number, side in enumerate(sides, 1):
        vote_path = review / f"vote-{number:03}.json"
        record = json.loads(vote_path.read_bytes())
        assert record["vote"] == {"join": "нет", "humor_A": "1", "humor_B": "1", "choice": "="}
        record["vote"]["choice"] = side
        write_json(vote_path, record)
    result = runner.invoke(app, ["score", str(run)])
    assert result.exit_code == 0
    score = json.loads(result.output)
    assert (score["wins"], score["losses"], score["ties"], score["accepted"]) == (0, 0, 40, False)
    assert score["should_join"] == {"нет": 40}
    assert score["humor"] == {"reference": {"1": 40}, "candidate": {"1": 40}}
