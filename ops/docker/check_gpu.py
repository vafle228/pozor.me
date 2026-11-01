"""Synthetic live queue/cancellation check; no dotenv, Telegram or private data."""

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

from pozor.config import Settings
from pozor.context import ContextPack, Evidence
from pozor.inference_queue import InferenceQueue
from pozor.llm import Generator, LlamaClient, messages_for


async def check(url: str) -> dict:
    settings = Settings(_env_file=None, llm_base_url=url)
    pack = ContextPack(chat_id=-1, thread_id=0, target_id=1, target_label="Synthetic",
        trigger=Evidence(evidence_id="trigger:1", chat_id=-1, allowed_target_id=1,
            source_namespace="request", relation="trigger", attribution="caller_hint",
            text="Обещаю выиграть в игре"))
    report = {"concurrency": {}, "cancellation": {}}
    async with LlamaClient(settings) as client:
        await client.check(time.monotonic() + 8)
        queue = InferenceQueue(client.wait_idle)
        generator = Generator(client)
        try:
            for count in (2, 4):
                rows = []

                async def submit(index, rows=rows):
                    started = time.monotonic()

                    async def generate(deadline):
                        queued_ms = (time.monotonic() - started) * 1000
                        result = await generator.generate(pack, deadline)
                        return {"queue_ms": round(queued_ms, 2), "fallback": result.fallback,
                                "total_ms": round((time.monotonic() - started) * 1000, 2)}

                    try:
                        rows.append(await queue.submit(key=f"synthetic:{index}", chat_id=-index-1,
                            factory=generate, deadline=started + 8))
                    except TimeoutError:
                        rows.append({"timeout": True})

                await asyncio.gather(*(submit(i) for i in range(count)))
                report["concurrency"][str(count)] = rows
        finally:
            await queue.aclose()

        # Exercise the production bounded request (180 tokens), not an unbounded
        # workload. A disconnected non-streaming request may finish server-side.
        request = asyncio.create_task(client.complete(messages_for(pack), time.monotonic() + 8))
        deadline = time.monotonic() + 5
        busy = False
        while time.monotonic() < deadline and not request.done():
            slots = await client.request("GET", "slots", deadline)
            if slots[0].get("is_processing"):
                busy = True
                break
            await asyncio.sleep(.01)
        started = time.monotonic()
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        await client.wait_idle(time.monotonic() + 8)
        report["cancellation"] = {"observed_busy": busy, "released": True,
                                  "release_ms": round((time.monotonic() - started) * 1000, 2)}
    return report


if __name__ == "__main__":
    result = asyncio.run(check(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
