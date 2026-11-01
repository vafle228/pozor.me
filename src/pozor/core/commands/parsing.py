from __future__ import annotations

import re
from dataclasses import dataclass

_COMMAND = re.compile(r"^/(?P<name>[A-Za-z_]+)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s|$)")


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str
    username_targets: tuple[str, ...]
    numeric_targets: tuple[int, ...] = ()
    target_labels: tuple[str, ...] = ()
    conversational: bool = False

    @property
    def target_count(self) -> int:
        return len(self.username_targets) + len(self.numeric_targets)


@dataclass(frozen=True)
class CommandContext:
    chat_id: int
    actor_id: int | None
    message_id: int
    thread_id: int
    text: str
    entities: tuple[dict[str, object], ...] = ()
    reply_user_id: int | None = None
    reply_is_bot: bool = False
    sender_is_bot: bool = False
    sender_is_anonymous: bool = False


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    text: str = ""
    session_id: str | None = None


@dataclass(frozen=True)
class SessionClock:
    """Immutable values required to make one deterministic session decision."""

    session_id: str
    chat_id: int
    thread_id: int
    target_id: int
    initiator_id: int
    revision: int
    evidence_epoch: int
    created_at: int
    last_sent_at: int | None
    sent_turns: int
    active: bool = True
    consenting: bool = True


@dataclass(frozen=True)
class IncomingEvent:
    chat_id: int
    thread_id: int
    actor_id: int
    addresses_session: bool
    stop: bool = False
    finish: bool = False
    admin: bool = False


class CommandError(RuntimeError):
    """A user-facing command precondition was not met."""


def session_close_reason(session: SessionClock, now: int) -> str | None:
    """Apply the bounded T2 clock to a session snapshot."""
    if not session.active:
        return "closed"
    if not session.consenting:
        return "opt_out"
    if session.sent_turns >= 4:
        return "turn_limit"
    if now >= session.created_at + 300:
        return "ttl"
    if session.last_sent_at is not None and now >= session.last_sent_at + 90:
        return "silence"
    return None


def decide_incoming(session: SessionClock, event: IncomingEvent, now: int) -> str:
    """Return the deterministic ignore/close/defer/continue transition."""
    if (event.chat_id, event.thread_id) != (session.chat_id, session.thread_id):
        return "ignore"
    reason = session_close_reason(session, now)
    if reason:
        return "close:" + reason
    authorized = event.actor_id in (session.target_id, session.initiator_id) or event.admin
    if authorized and (event.stop or event.finish):
        return "close:explicit"
    if event.actor_id not in (session.target_id, session.initiator_id) or not event.addresses_session:
        return "ignore"
    if session.last_sent_at is None:
        return "ignore"
    if now < session.last_sent_at + 10:
        return "defer"
    return "continue"


def _utf16_slice(text: str, offset: int, length: int) -> str | None:
    if offset < 0 or length < 1:
        return None
    raw = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    if end > len(raw):
        return None
    try:
        return raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def parse_command(
    text: str,
    entities: tuple[dict[str, object], ...] = (),
    *,
    bot_username: str | None = None,
) -> ParsedCommand | None:
    """Parse a leading bot command and collect entity-backed username targets."""
    match = _COMMAND.match(text)
    mention_call = None
    if match is None and bot_username:
        mention_call = re.match(
            r"^@" + re.escape(bot_username)
            + r"(?=\s|$)(?:\s+(?:попусти|прожарь|roast)(?=\s|$))?\s*",
            text, re.IGNORECASE,
        )
        if mention_call and not any(
            e.get("type") == "mention" and e.get("offset") == 0
            and e.get("length") == len(bot_username) + 1
            for e in entities
        ):
            mention_call = None
    if match is None and mention_call is None:
        return None
    name = match.group("name").casefold() if match else "roast"
    # Do not interpret commands explicitly addressed to a different bot.
    addressed_to = match.group("bot") if match else None
    if addressed_to is not None and (
        bot_username is None or addressed_to.casefold() != bot_username.casefold()
    ):
        return None
    targets: list[str] = []
    numeric_targets: list[int] = []
    target_labels: list[str] = []
    for entity in entities:
        if entity.get("type") not in {"mention", "text_mention"}:
            continue
        offset, length = entity.get("offset"), entity.get("length")
        if mention_call and offset == 0:
            continue
        if type(offset) is not int or type(length) is not int:
            continue
        fragment = _utf16_slice(text, offset, length)
        if entity.get("type") == "text_mention":
            user = entity.get("user")
            user_id = user.get("id") if isinstance(user, dict) else None
            if fragment is not None and type(user_id) is int and user_id > 0:
                numeric_targets.append(user_id)
                target_labels.append(fragment)
        elif fragment and fragment.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", fragment):
            targets.append(fragment[1:].casefold())
    consumed = match.end() if match else mention_call.end()  # type: ignore[union-attr]
    return ParsedCommand(
        name,
        text[consumed:].strip(),
        tuple(dict.fromkeys(targets)),
        tuple(dict.fromkeys(numeric_targets)),
        tuple(dict.fromkeys(target_labels)),
        bool(mention_call and not re.search(r"\s(?:попусти|прожарь|roast)(?=\s|$)", mention_call.group(), re.IGNORECASE)),
    )


def roast_hint(command: ParsedCommand, target_label: str = '') -> str:
    hint = command.argument
    for label in command.target_labels:
        hint = hint.replace(label, target_label, 1)
    for username in command.username_targets:
        hint = re.sub(r'@' + re.escape(username) + r'\b', lambda _: target_label, hint, flags=re.IGNORECASE)
    return hint.strip()


