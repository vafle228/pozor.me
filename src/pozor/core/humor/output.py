import re

from pydantic import ValidationError

from pozor.core.domain.context import ContextPack
from pozor.core.domain.generation import GeneratedRoast, InvalidOutput
from pozor.core.humor.material import normalize


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
    allowed.update(f'message:{hook.source_id}' for hook in pack.hooks)
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
    address_prefix = re.compile(r'^' + re.escape(roast_label(pack)) + r'\s*[,::—-]\s*', re.IGNORECASE)
    answer = normalize(address_prefix.sub('', roast.text))
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
    previous += pack.repetition_texts
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


