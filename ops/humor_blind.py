"""Локальное слепое сравнение двух готовых серий ответов на одних диалогах."""

import hashlib
import json
import random
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, Field, StringConstraints, TypeAdapter

from pozor.core.humor.evaluation import Comparison

app = typer.Typer()
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Side = Literal["A", "B"]


class Line(BaseModel):
    author: Text
    text: Text


class Scene(BaseModel):
    id: Text
    context: list[Line] = Field(min_length=2)


class Series(BaseModel):
    variant: Text
    suite_sha256: Text
    responses: dict[str, Text]


class Pair(BaseModel):
    context: list[Line]
    A: Text
    B: Text


class Vote(BaseModel):
    join: Literal["да", "нет", "?"]
    humor_A: Literal["0", "1", "2"]
    humor_B: Literal["0", "1", "2"]
    choice: Literal["A", "B", "="]


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def distinct_contexts(rows: Sequence[Scene | Pair]) -> None:
    conversations = {tuple((line.author, line.text) for line in row.context) for row in rows}
    if len(conversations) != len(rows):
        raise typer.BadParameter("Каждый диалог должен иметь уникальный контекст.")


@app.command()
def pack(dialogs: Path, reference: Path, candidate: Path, output: Path) -> None:
    """Собрать новый слепой бланк и отдельный ключ из двух полных серий ответов."""
    raw = dialogs.read_bytes()
    scenes = [Scene.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    ids = {s.id for s in scenes}
    if not scenes or len(ids) != len(scenes):
        raise typer.BadParameter("Нужны диалоги с уникальными ID.")
    distinct_contexts(scenes)
    series = [Series.model_validate_json(path.read_bytes()) for path in (reference, candidate)]
    if any(s.suite_sha256 != digest(raw) or set(s.responses) != ids for s in series):
        raise typer.BadParameter("У обеих серий должны совпадать hash набора и все ID диалогов.")
    if series[0].variant == series[1].variant:
        raise typer.BadParameter("Укажите разные версии сравниваемых вариантов.")

    rng = random.SystemRandom()
    rng.shuffle(scenes)
    sides: list[Side] = ["A", "B"] * (len(scenes) // 2)
    if len(scenes) % 2:
        sides.append(rng.choice(["A", "B"]))
    rng.shuffle(sides)
    booklet = []
    for scene, side in zip(scenes, sides, strict=True):
        texts = [s.responses[scene.id] for s in series]
        if side == "A":
            texts.reverse()
        booklet.append(Pair(context=scene.context, A=texts[0], B=texts[1]).model_dump())
    content = encode(booklet).encode()
    key = {
        "booklet_sha256": digest(content),
        "suite_sha256": digest(raw),
        "reference": series[0].variant,
        "candidate": series[1].variant,
        "cases": [s.id for s in scenes],
        "candidate_side": sides,
    }
    # Папка появляется только после записи и ключа, и бланка на том же томе.
    if output.exists():
        raise typer.BadParameter("Каталог этого сравнения уже существует.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        staging = Path(scratch) / "run"
        (staging / "review").mkdir(parents=True)
        (staging / "key").mkdir()
        (staging / "review" / "booklet.json").write_bytes(content)
        (staging / "key" / "key.json").write_text(encode(key), encoding="utf-8")
        staging.rename(output)
    typer.echo(f"Пар в бланке: {len(scenes)}. Оценщику нужен каталог review.")


def read_votes(review_dir: Path, booklet_hash: str, count: int) -> dict[int, Vote]:
    votes = {}
    for path in review_dir.glob("vote-*.json"):
        record = json.loads(path.read_bytes())
        number = record["pair"]
        if (
            record["booklet_sha256"] != booklet_hash
            or type(number) is not int
            or not 1 <= number <= count
            or number in votes
        ):
            raise typer.BadParameter(
                "Голоса относятся к другому бланку или содержат неверную пару."
            )
        votes[number] = Vote.model_validate(record["vote"])
    return votes


def ask(question: str, answers: tuple[str, ...]) -> str:
    while True:
        answer = str(typer.prompt(question)).strip()
        if answer in answers:
            return answer
        typer.echo("Варианты ввода: " + ", ".join(answers))


@app.command()
def review(review_dir: Path) -> None:
    """Оценить пары за один или несколько подходов. Ctrl+C завершает сеанс."""
    booklet = (review_dir / "booklet.json").read_bytes()
    pairs = TypeAdapter(list[Pair]).validate_json(booklet)
    distinct_contexts(pairs)
    booklet_hash = digest(booklet)
    votes = read_votes(review_dir, booklet_hash, len(pairs))
    for number, pair in enumerate(pairs, 1):
        if number in votes:
            continue
        typer.echo(f"\nРазговор {number}/{len(pairs)}")
        for line in pair.context:
            typer.echo(f"{line.author} — {line.text}")
        join = ask("Стоило ли боту сейчас вступать? (да / нет / ?)", ("да", "нет", "?"))
        for side in ("A", "B"):
            typer.echo(f"\n{side} — {getattr(pair, side)}")
        typer.echo("Для каждого текста: 0 — не смешно, 1 — улыбнуло, 2 — рассмешило.")
        if pair.A == pair.B:
            typer.echo("Тексты совпали. В сравнении будет ничья.")
        vote = Vote.model_validate(
            {
                "join": join,
                "humor_A": ask("Реакция на A", ("0", "1", "2")),
                "humor_B": ask("Реакция на B", ("0", "1", "2")),
                "choice": "="
                if pair.A == pair.B
                else ask("Какой ответ лучше в этой беседе? (A / B / =)", ("A", "B", "=")),
            }
        )
        record = {"booklet_sha256": booklet_hash, "pair": number, "vote": vote.model_dump()}
        with tempfile.TemporaryDirectory(dir=review_dir) as scratch:
            path = Path(scratch) / "vote.json"
            path.write_text(encode(record), encoding="utf-8")
            path.replace(review_dir / f"vote-{number:03}.json")
        typer.echo("Голос сохранён.")
    typer.echo("Все пары оценены.")


@app.command()
def score(run: Path) -> None:
    """Раскрыть варианты после завершения бланка и подсчитать человеческие оценки."""
    review_dir = run / "review"
    booklet = (review_dir / "booklet.json").read_bytes()
    pairs = TypeAdapter(list[Pair]).validate_json(booklet)
    distinct_contexts(pairs)
    count = len(pairs)
    key = json.loads((run / "key" / "key.json").read_bytes())
    sides = TypeAdapter(list[Side]).validate_python(key["candidate_side"])
    if key["booklet_sha256"] != digest(booklet) or len(sides) != count:
        raise typer.BadParameter("Ключ и бланк от разных сравнений.")
    votes = read_votes(review_dir, digest(booklet), count)
    if len(votes) != count:
        typer.echo(f"Оценено {len(votes)} из {count}. Продолжите оценку в каталоге review.")
        return
    wins = ties = 0
    humor: dict[str, Counter[str]] = {"candidate": Counter(), "reference": Counter()}
    for number, vote in votes.items():
        side = sides[number - 1]
        pair = pairs[number - 1]
        choice = "=" if pair.A == pair.B else vote.choice
        wins += choice == side
        ties += choice == "="
        reactions = {"A": vote.humor_A, "B": vote.humor_B}
        humor["candidate"][reactions.pop(side)] += 1
        humor["reference"][next(iter(reactions.values()))] += 1
    result = Comparison(count, wins, count - wins - ties, ties)
    typer.echo(
        encode(
            {
                "candidate": key["candidate"],
                "reference": key["reference"],
                "pairs": count,
                "wins": wins,
                "losses": result.losses,
                "ties": ties,
                "win_rate": result.win_rate,
                "accepted": result.accepted,
                "humor": humor,
                "should_join": Counter(v.join for v in votes.values()),
            }
        )
    )


if __name__ == "__main__":
    app()
