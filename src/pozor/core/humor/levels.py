import re


def requested_level(argument: str) -> int:
    if re.search(r'\b(?:мягко|дружески|легко)\b',argument,re.IGNORECASE):
        return 1
    # An omitted modifier imposes no additional ceiling.
    return 3


def effective_level(chat: int, requested: int, consent: int) -> int:
    if any(level not in (1,2,3) for level in (chat,requested,consent)):
        raise ValueError('level must be between 1 and 3')
    return min(chat,requested,consent)
