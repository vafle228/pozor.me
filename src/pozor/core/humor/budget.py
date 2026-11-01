import json
from collections.abc import Callable

from pozor.core.domain.context import ContextPack
from pozor.core.domain.generation import InferenceError
from pozor.core.humor.material import normalize
from pozor.core.humor.prompts import messages_for
from pozor.core.ports import ModelPort


async def pack_context(client: ModelPort, pack: ContextPack, deadline: float, *,
                       builder: Callable[[ContextPack], list[dict[str, str]]] = messages_for) -> ContextPack:
    """Count serialized metadata, then verify the complete templated prompt."""
    data = pack.model_dump()
    data["recent"], data["evidence"], data["session_turns"], data['conversation'], data['dossier'] = [], [], [], [], []
    seen = {normalize(pack.trigger.text)}
    topics: dict[str, int] = {}
    for name, rows, budget in (
        ("recent", list(reversed(pack.recent)), client.settings.recent_token_budget),
        ("evidence", pack.evidence, client.settings.historical_token_budget),
    ):
        used = 0
        for item in rows:
            text = normalize(item.text)
            if not text or text in seen:
                continue
            if name == "evidence" and item.topic != "general" and topics.get(item.topic, 0) >= client.settings.per_topic_limit:
                continue
            cost = await client.tokens(item.model_dump_json(exclude_defaults=True), deadline)
            if used + cost > budget:
                continue
            used += cost
            data[name].append(item.model_dump())
            seen.add(text)
            if name == "evidence":
                topics[item.topic] = topics.get(item.topic, 0) + 1
        if name == "recent":
            data[name].reverse()
    used = 0
    for turn in reversed(pack.session_turns):
        cost = await client.tokens(json.dumps(turn, ensure_ascii=False), deadline)
        if used + cost > client.settings.session_token_budget:
            break
        data["session_turns"].insert(0, turn)
        used += cost
    used = 0
    for exchange in reversed(pack.conversation):
        cost = await client.tokens(exchange.model_dump_json(), deadline)
        if used + cost > client.settings.conversation_token_budget:
            break
        data['conversation'].insert(0, exchange.model_dump())
        used += cost
    for memory in pack.dossier:
        if await client.tokens(memory.model_dump_json(), deadline) <= client.settings.dossier_token_budget:
            data['dossier'].append(memory.model_dump())
    while True:
        result = ContextPack.model_validate(data)
        count = await client.count(builder(result), deadline)
        if count + client.settings.max_output_tokens + client.settings.token_reserve <= client.settings.context_size:
            return result
        for name in ("evidence", "dossier", "session_turns", "recent", "conversation"):
            if data[name]:
                data[name].pop(-1 if name == "evidence" else 0)
                break
        else:
            raise InferenceError("context_overflow")


