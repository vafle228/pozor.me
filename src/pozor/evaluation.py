"""Offline retrieval evaluation and explicit opt-in local model benchmark.

Only frozen synthetic cases are shipped. Humor and factual attribution need a
human review; a matching evidence ID is not proof that prose is grounded.
"""

from __future__ import annotations

import asyncio
import hashlib
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from pozor.config import Settings
from pozor.context import ContextPack, Contract, retrieve
from pozor.llm import Generator, LlamaClient
from pozor.storage import connect, migrate


class EvalQuote(Contract):
    text: str
    author: int = 42
    target: int = 42
    chat: int = -1
    relation: Literal["self", "mention", "reply", "context"] = "self"


class EvalCase(Contract):
    id: str
    category: Literal["callback", "empty", "identity", "adversarial", "control"]
    hint: str
    quotes: list[EvalQuote] = Field(default_factory=list, max_length=40)
    expected: list[int] = Field(default_factory=list)
    action: Literal["none", "stop", "edit", "forget", "deadline", "optout"] = "none"


def load_suite(path: Path) -> list[EvalCase]:
    cases = [
        EvalCase.model_validate_json(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    if len(cases) < 30 or len({c.id for c in cases}) != len(cases):
        raise ValueError("at least 30 unique synthetic cases required")
    return cases


def prepare(database: Path, case: EvalCase) -> ContextPack:
    migrate(database)
    with connect(database) as db:
        db.execute("INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1),(-2,1,1)")
        for number, quote in enumerate(case.quotes, 1):
            attr = (
                "observed_self"
                if quote.author == quote.target and quote.relation == "self"
                else "third_party"
            )
            db.execute(
                "INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,"
                "author_id,text,text_hash,occurred_at,origin) VALUES (?,?,?,?,?,?,?,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'live')",
                (
                    number,
                    quote.chat,
                    f"live:{quote.chat}",
                    number,
                    quote.author,
                    quote.text,
                    hashlib.sha256(quote.text.encode()).hexdigest(),
                ),
            )
            db.execute(
                "INSERT INTO message_subjects VALUES (?,?,?,?)",
                (number, quote.target, quote.relation, attr),
            )
    return retrieve(
        database,
        chat_id=-1,
        thread_id=0,
        target_id=42,
        trigger_id=999,
        hint=case.hint,
        target_label="Synthetic 42",
    )


def control_check(database: Path, case: EvalCase) -> bool | None:
    if case.action == "none":
        return None
    from pozor.commands import close_session, set_consent, start_session
    from pozor.delivery import guard
    from pozor.privacy import bump, forget

    now = int(time.time())
    with connect(database) as db:
        db.execute("INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1)")
    sid = start_session(
        database,
        chat_id=-1,
        thread_id=0,
        target_id=42,
        initiator_id=7,
        trigger_message_id=999,
        now=now,
    )
    with connect(database) as db:
        db.execute(
            "INSERT INTO turns(session_id,turn_no,revision,evidence_epoch,deadline,status) "
            "SELECT id,1,revision,0,?,'ready' FROM sessions WHERE id=?",
            (now + 8, sid),
        )
        turn = db.execute("SELECT * FROM turns WHERE session_id=?", (sid,)).fetchone()
        if not guard(db, turn, now):
            return False
    if case.action == "stop":
        close_session(database, session_id=sid, chat_id=-1, thread_id=0, actor_id=42)
    elif case.action == "edit":
        with connect(database) as db:
            bump(db, -1, 42)
    elif case.action == "forget":
        forget(database, -1, 42)
    elif case.action == "optout":
        set_consent(database, -1, 42, False)
    with connect(database) as db:
        return not guard(db, turn, now + 9 if case.action == "deadline" else now)


async def benchmark(suite: Path, *, live: bool = False, repeat: int = 1) -> dict[str, Any]:
    if repeat < 1:
        raise ValueError("repeat must be positive")
    cases = load_suite(suite)
    if live:
        repeat = max(2, repeat)  # 25 model cases × 2 plus separate deterministic controls.
    report: dict[str, Any] = {
        "suite_sha256": hashlib.sha256(suite.read_bytes()).hexdigest(),
        "mode": "local_model" if live else "offline_retrieval",
        "human_review": "pending",
        "humor_gate": "not_evaluated",
        "gpu_backend_model_build": "operator must record deployment-lock and hardware",
        "queue_wait_ms": None,
        "concurrency": "not_measured",
        "repeat": repeat,
        "coverage_scope": "retrieval candidates before model token packing",
        "results": [],
    }
    durations: list[float] = []
    good = total = 0
    with tempfile.TemporaryDirectory(prefix="pozor-eval-") as directory:
        packs = []
        for index, case in enumerate(cases):
            before = time.perf_counter()
            pack = prepare(Path(directory) / f"{index}.sqlite3", case)
            elapsed = (time.perf_counter() - before) * 1000
            # Includes setup/migrations: never label this as query-only or GPU latency.
            ids = {e.evidence_id for e in [*pack.recent, *pack.evidence]}
            expected = {f"message:{number}" for number in case.expected}
            entitled = {
                f"message:{n}"
                for n, q in enumerate(case.quotes, 1)
                if q.chat == -1 and q.target == 42
            }
            control = control_check(Path(directory) / f"{index}.sqlite3", case)
            passed = expected <= ids <= entitled and control is not False
            if expected:
                total += 1
                good += int(passed)
            report["results"].append(
                {
                    "id": case.id,
                    "category": case.category,
                    "retrieval_pass": passed,
                    "control_pass": control,
                    "expected_ids": sorted(expected),
                    "retrieved_ids": sorted(ids),
                    "setup_and_retrieval_ms": elapsed,
                    "generations": [],
                    "human_scores_0_to_2": {
                        "relevance": None,
                        "callback": None,
                        "brevity": None,
                        "humor": None,
                    },
                    "human_safety_pass": None,
                }
            )
            packs.append(pack)
        if live:
            settings = Settings()
            async with LlamaClient(settings) as client:
                await client.check(asyncio.get_running_loop().time() + 8)
                generator = Generator(client)
                for _ in range(3):
                    await generator.generate(packs[0], asyncio.get_running_loop().time() + 8)
                for index, pack in enumerate(packs):
                    if cases[index].category == "control":
                        continue
                    for _ in range(repeat):
                        before = time.perf_counter()
                        result = await generator.generate(
                            pack, asyncio.get_running_loop().time() + 8
                        )
                        elapsed = (time.perf_counter() - before) * 1000
                        durations.append(elapsed)
                        report["results"][index]["generations"].append(
                            {
                                "text": result.roast.text,
                                "evidence_ids": result.roast.evidence_ids,
                                "characters": len(result.roast.text),
                                "fallback": result.fallback,
                                "generation_ms": elapsed,
                            }
                        )
    report["evidence_coverage"] = good / total if total else None
    model_cases = [r for r in report["results"] if r["expected_ids"] and r["generations"]]
    report["generated_evidence_coverage"] = (
        sum(
            any(set(g["evidence_ids"]) & set(r["expected_ids"]) for g in r["generations"])
            for r in model_cases
        )
        / len(model_cases)
        if model_cases
        else None
    )
    report["retrieval_pass"] = all(r["retrieval_pass"] for r in report["results"])
    report["model_latency_ms"] = (
        {
            "p50": statistics.median(durations),
            "p95": sorted(durations)[max(0, int(len(durations) * 0.95 + 0.999) - 1)],
        }
        if durations
        else None
    )
    return report
