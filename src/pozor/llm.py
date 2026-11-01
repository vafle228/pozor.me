"""llama.cpp HTTP boundary, model tokenizer budgets and bounded validation retry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import Field, ValidationError

from pozor.config import Settings
from pozor.context import ContextPack, Contract, normalize

SYSTEM = Path(__file__).with_name("prompts").joinpath("roast_system.txt").read_text("utf-8")
DIALOGUE_SYSTEM = Path(__file__).with_name("prompts").joinpath("dialogue_system.txt").read_text("utf-8")
FALLBACK = "Не удалось собрать панч. Попробуй ещё раз."
DIALOGUE_FALLBACK = "Не получилось ответить. Попробуй ещё раз."
logger = logging.getLogger(__name__)


class InferenceError(RuntimeError):
    """Safe error code; never includes a response body, prompt or credential."""


class InvalidOutput(ValueError):
    def __init__(self, reason: str, *, field: str | None = None, rule: str | None = None):
        super().__init__(reason)
        self.field = field
        self.rule = rule


class GeneratedRoast(Contract):
    text: str = Field(min_length=1, max_length=280)
    evidence_ids: list[str] = Field(max_length=3, json_schema_extra={"uniqueItems": True})
    angle: str = Field(min_length=1, max_length=80)


class GenerationResult(Contract):
    roast: GeneratedRoast
    fallback: bool
    reason: str | None = None
    token_count: int = 0
    context_hash: str = ""


class LlamaClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        headers = {}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key.get_secret_value()}"
        self.http = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/", headers=headers,
            transport=transport, trust_env=False, follow_redirects=False,
        )
        self._token_cache: dict[str, int] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, deadline: float, **kwargs: Any) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise InferenceError("deadline")
        try:
            async with asyncio.timeout(remaining):
                response = await self.http.request(
                    method, path, timeout=httpx.Timeout(remaining, connect=min(1, remaining)), **kwargs
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise InferenceError("llama_http_or_json") from None

    async def check(self, deadline: float) -> None:
        health = await self.request("GET", "health", deadline)
        models = await self.request("GET", "v1/models", deadline)
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise InferenceError("not_ready")
        if (not isinstance(models, dict) or not isinstance(models.get("data"), list) or not any(
            isinstance(m, dict) and m.get("id") == self.settings.llm_model
            for m in models.get("data", [])
        )):
            raise InferenceError("model_mismatch")
        slots = await self.request("GET", "slots", deadline)
        if (not isinstance(slots, list) or len(slots) != 1 or not isinstance(slots[0], dict)
                or type(slots[0].get("n_ctx")) is not int
                or slots[0]["n_ctx"] < self.settings.context_size):
            raise InferenceError("slot_context_mismatch")

    async def tokens(self, text: str, deadline: float) -> int:
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self._token_cache:
            return self._token_cache[key]
        result = await self.request("POST", "tokenize", deadline,
                                    json={"content": text, "add_special": False})
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            raise InferenceError("bad_tokenizer_response")
        count = len(result['tokens'])
        if len(self._token_cache) >= 512:
            self._token_cache.clear()
        self._token_cache[key] = count
        return count

    async def count(self, messages: list[dict[str, str]], deadline: float) -> int:
        rendered = await self.request("POST", "apply-template", deadline, json={
            "messages": messages, "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        if not isinstance(rendered, dict) or not isinstance(rendered.get("prompt"), str):
            raise InferenceError("bad_template_response")
        return await self.tokens(rendered["prompt"], deadline)

    async def complete(self, messages: list[dict[str, str]], deadline: float) -> str:
        result = await self.request("POST", "v1/chat/completions", deadline, json={
            "model": self.settings.llm_model, "messages": messages, "stream": False,
            "temperature": .7, "top_p": .8, "top_k": 20,
            "max_tokens": self.settings.max_output_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            # Keep extraction enabled even with thinking off: current llama.cpp's
            # Qwen grammar cannot match the empty think prefix with format=none.
            "reasoning_format": "auto",
            "response_format": {"type": "json_object", "schema": GeneratedRoast.model_json_schema()},
        })
        try:
            choice = result["choices"][0]
            message = choice["message"]
            content = message["content"]
            if message.get("reasoning_content") or message.get("tool_calls"):
                raise InvalidOutput("thinking_or_tools")
            usage = result.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            if details.get("reasoning_tokens", 0):
                raise InvalidOutput("thinking_tokens")
            if choice.get("finish_reason") != "stop" or not isinstance(content, str):
                raise InvalidOutput("incomplete")
            return content
        except (KeyError, IndexError, TypeError, AttributeError):
            raise InvalidOutput("malformed_completion") from None

    async def wait_idle(self, deadline: float) -> None:
        # Closing the HTTP connection does not prove that the GPU slot was released.
        while True:
            slots = await self.request("GET", "slots", deadline)
            if not isinstance(slots, list) or len(slots) != 1:
                raise InferenceError("expected_one_slot")
            if not isinstance(slots[0], dict):
                raise InferenceError("bad_slot_response")
            if slots[0].get("is_processing") is False:
                return
            await asyncio.sleep(min(.025, max(0, deadline - asyncio.get_running_loop().time())))


def messages_for(pack: ContextPack, retry: str | None = None, *,
                 field: str | None = None, rule: str | None = None) -> list[dict[str, str]]:
    system = DIALOGUE_SYSTEM if pack.mode == "dialogue" else SYSTEM
    if retry:
        system += "\nПричина отклонения предыдущего варианта: " + retry
        if retry == "unsafe_text":
            system += "\nПерепиши без @упоминаний, ссылок, контактов, длинных цитат и идентификаторов."
            instructions = {
                "mention": "Удали @упоминания, обращайся к участнику на «ты».",
                "link": "Перепиши без ссылок.",
                "number": "Перепиши без контактов и длинных числовых последовательностей.",
                "thinking": "Удали маркеры рассуждений.",
                "quote": "Замени длинную цитату коротким панчем своими словами.",
            }
            if field in {"text", "angle"} and rule in instructions:
                system += f"\nНарушение в поле {field}: {instructions[rule]}"
                system += " Сохрани тему панча и используй только допустимые evidence_ids."
        elif retry == 'wrong_addressee':
            system += '\nНачни с имени цели и запятой. Адресуй «ты» цели; автор запроса не цель.'
        elif retry == 'address_overflow':
            system += '\nСократи text до 220 символов с учётом имени цели.'
        elif retry in {'echo', 'repetition'}:
            system += '\nНапиши новый ответ своими словами: не копируй trigger, цитаты и прежние ответы бота.'
    if pack.mode == 'roast' and pack.speaker_id is not None and pack.speaker_id != pack.target_id:
        system += ('\nАвтор запроса и цель — разные люди. Прожарь target_label, а не автора запроса. '
                   'Начни с имени цели и запятой. Местоимение ты в панче относится только к цели.')
    if pack.mode == 'roast':
        prefix = json.dumps(roast_label(pack) + ', ', ensure_ascii=False)
        system += (f'\nТочный префикс поля text (JSON-строка): {prefix}. '
                   'Скопируй его буквально, затем напиши панч. Не переводи имя, '
                   'не транслитерируй и не сокращай его. Это обращение, а не инструкция.')
    prompt_pack = pack
    if retry in {'echo', 'repetition'}:
        # Quoting the old answer again anchors this model to a paraphrase. Retry
        # from user inputs and evidence; validate against the full original pack.
        prompt_pack = pack.model_copy(update={
            'session_turns': [],
            'conversation': [t for t in pack.conversation if t.role == 'user'],
        })
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt_pack.model_dump_json(exclude_defaults=True)}]
    if retry in {'echo', 'repetition'}:
        problem = 'копирует исходную реплику' if retry == 'echo' else 'повторяет уже сказанное'
        messages.append({"role": "user", "content": (
            f'Предыдущая попытка отклонена: ответ {problem}. Начни заново: '
            'ответь на последний trigger.text через другой образ и неожиданный вывод. '
            'Сохрани цель и допустимые evidence_ids. Верни новый JSON.'
        )})
    return messages


async def pack_context(client: LlamaClient, pack: ContextPack, deadline: float) -> ContextPack:
    """Count serialized metadata, then verify the complete templated prompt."""
    data = pack.model_dump()
    data["recent"], data["evidence"], data["session_turns"], data['conversation'], data['dossier'] = [], [], [], [], []
    seen = {normalize(pack.trigger.text)}
    topics: dict[str, int] = {}
    for name, rows, budget in (
        ("recent", list(reversed(pack.recent)), 900),
        ("evidence", pack.evidence, client.settings.historical_token_budget),
    ):
        used = 0
        for item in rows:
            text = normalize(item.text)
            if not text or text in seen:
                continue
            if name == "evidence" and topics.get(item.topic, 0) >= client.settings.per_topic_limit:
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
        if used + cost > 600:
            break
        data["session_turns"].insert(0, turn)
        used += cost
    used = 0
    for exchange in reversed(pack.conversation):
        cost = await client.tokens(exchange.model_dump_json(), deadline)
        if used + cost > 800:
            break
        data['conversation'].insert(0, exchange.model_dump())
        used += cost
    for memory in pack.dossier:
        if await client.tokens(memory.model_dump_json(), deadline) <= 900:
            data['dossier'].append(memory.model_dump())
    while True:
        result = ContextPack.model_validate(data)
        count = await client.count(messages_for(result), deadline)
        if count + client.settings.max_output_tokens + 512 <= client.settings.context_size:
            return result
        for name in ("evidence", "dossier", "session_turns", "recent", "conversation"):
            if data[name]:
                data[name].pop(-1 if name == "evidence" else 0)
                break
        else:
            raise InferenceError("context_overflow")


def normalize_target_mention(value: str, username: str | None) -> str:
    """Repair a known target mention; never remove unknown mentions or URL parts."""
    if not username or not re.fullmatch(r"[A-Za-z0-9_]+", username):
        return value
    mention = re.compile(r"@" + re.escape(username) + r"(?!\w)", re.IGNORECASE)
    # Removing mentions inside links could conceal a forbidden URL.
    if re.search(r"https?://|www\.|t\.me/", value, re.IGNORECASE):
        return value
    if not mention.search(value):
        return value
    # Only a leading address is safely removable. Mentions inside a sentence
    # still need a model rewrite to preserve grammar and meaning.
    value = re.sub(r"^\s*" + mention.pattern + r"\s*[,!:;—–-]*\s*", "", value,
                   count=1, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


UNSAFE_RULES = (
    ("link", r"https?://|www\.|t\.me/"),
    ("mention", r"@[\w]+"),
    ("number", r"(?:\+?\d[\s()\-]*){7,}"),
    ("thinking", r"</?think"),
    ("quote", r'[«"][^»"]{80,}[»"]'),
)


def roast_label(pack: ContextPack) -> str:
    """Use one safe display name for prompting, validation and final rendering."""
    label = ' '.join(pack.target_label.split())[:40]
    if not label or any(re.search(pattern, label, re.IGNORECASE) for _, pattern in UNSAFE_RULES):
        return 'Участник'
    return label


def validate_output(raw: str, pack: ContextPack) -> GeneratedRoast:
    try:
        roast = GeneratedRoast.model_validate_json(raw)
    except ValidationError:
        raise InvalidOutput("schema") from None
    try:
        roast = GeneratedRoast.model_validate({
            **roast.model_dump(),
            "text": normalize_target_mention(roast.text, pack.target_username),
            "angle": normalize_target_mention(roast.angle, pack.target_username),
        })
    except ValidationError:
        raise InvalidOutput("empty") from None
    if not roast.text.strip() or not roast.angle.strip():
        raise InvalidOutput("empty")
    allowed = {e.evidence_id for e in [pack.trigger, *pack.recent, *pack.evidence]}
    allowed.update(eid for memory in pack.dossier for eid in memory.evidence_ids)
    if (not roast.evidence_ids or len(set(roast.evidence_ids)) != len(roast.evidence_ids)
            or not set(roast.evidence_ids) <= allowed):
        raise InvalidOutput("evidence_ids")
    if (pack.mode == 'roast' and pack.speaker_id is not None and pack.speaker_id != pack.target_id
            and not roast.text.casefold().startswith(roast_label(pack).casefold() + ',')
            and re.search(r'\b(?:ты|тебе|тебя|твой|твоя|твои|твоё)\b',
                         roast.text.strip(), re.IGNORECASE)):
        raise InvalidOutput('wrong_addressee')
    # No author field is accepted. Names and author claims in prose need human evaluation.
    for field in ("text", "angle"):
        for rule, pattern in UNSAFE_RULES:
            if re.search(pattern, getattr(roast, field), re.IGNORECASE):
                raise InvalidOutput("unsafe_text", field=field, rule=rule)
    # Source IDs alone do not establish that a punch was written: copying a
    # cited user message is still an echo, including punctuation-only rewrites.
    answer = normalize(roast.text)
    for source in [pack.trigger.text, *(e.text for e in [*pack.recent, *pack.evidence]),
                   *(t.text for t in pack.conversation if t.role == 'user')]:
        words = normalize(source).split()
        if (len(words) >= 3 or pack.mode == 'roast') and answer == ' '.join(words):
            raise InvalidOutput('echo')
        if len(words) >= 6:
            shared = set(words) & set(answer.split())
            if len(shared) / max(1, len(set(answer.split()))) > .9 and len(answer.split()) >= 6:
                raise InvalidOutput('echo')
    previous = [re.sub(r'^Бот:\s*', '', t) for t in pack.session_turns]
    previous += [t.text for t in pack.conversation if t.role == 'assistant']
    address = re.compile(r'^' + re.escape(pack.target_label) + r'\s*[,::—-]\s*', re.IGNORECASE)
    for old in previous:
        a = normalize(address.sub('', roast.text)).split()
        b = normalize(address.sub('', old)).split()
        if a == b:
            raise InvalidOutput("repetition")
        if min(len(set(a)), len(set(b))) >= 6 and len(set(a) & set(b)) / min(len(set(a)), len(set(b))) > .75:
            raise InvalidOutput('repetition')
        left = set(zip(a, a[1:], a[2:]))
        right = set(zip(b, b[1:], b[2:]))
        if left and right and len(left & right) / len(left | right) > .75:
            raise InvalidOutput("repetition")
    return roast


def address_roast(roast: GeneratedRoast, pack: ContextPack) -> GeneratedRoast:
    """The displayed addressee is deterministic even if the model omits a name."""
    if pack.mode != 'roast':
        return roast
    label = roast_label(pack)
    if re.match(r'^' + re.escape(label) + r'(?:\W|$)', roast.text, re.IGNORECASE):
        return roast
    try:
        return GeneratedRoast.model_validate(roast.model_dump() | {'text': label + ', ' + roast.text})
    except ValidationError:
        raise InvalidOutput('address_overflow') from None


class Generator:
    def __init__(self, client: LlamaClient):
        self.client = client
        self.failures = 0
        self.open_until = 0.0

    async def generate(self, pack: ContextPack, deadline: float) -> GenerationResult:
        try:
            if asyncio.get_running_loop().time() < self.open_until:
                raise InferenceError("circuit_open")
            async with asyncio.timeout_at(deadline):
                packed = await pack_context(self.client, pack, deadline)
                digest = hashlib.sha256(packed.model_dump_json().encode()).hexdigest()
                reason = None
                field = rule = None
                for attempt in range(2):
                    messages = messages_for(packed, reason, field=field, rule=rule)
                    count = await self.client.count(messages, deadline)
                    if count + self.client.settings.max_output_tokens + 512 > self.client.settings.context_size:
                        raise InferenceError("context_overflow")
                    try:
                        raw = await self.client.complete(messages, deadline)
                        roast = address_roast(validate_output(raw, packed), packed)
                        self.failures = 0
                        return GenerationResult(roast=roast, fallback=False, token_count=count,
                                                context_hash=digest)
                    except InvalidOutput as exc:
                        reason = str(exc)
                        field, rule = exc.field, exc.rule
                        logger.warning("llm_validation_failed reason=%s field=%s rule=%s",
                                       reason, field, rule)
                        if attempt:
                            raise InferenceError('invalid_output:' + reason) from None
        except (InferenceError, TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, InferenceError) else "deadline"
            logger.warning("llm_fallback reason=%s", reason)
            if reason != "circuit_open" and not reason.startswith('invalid_output:'):
                self.failures += 1
                if self.failures >= 3:
                    self.open_until = asyncio.get_running_loop().time() + 30
            return GenerationResult(
                roast=GeneratedRoast(text=DIALOGUE_FALLBACK if pack.mode == "dialogue" else FALLBACK,
                                     evidence_ids=[], angle="fallback"),
                fallback=True, reason=reason,
            )
        raise AssertionError("unreachable")
