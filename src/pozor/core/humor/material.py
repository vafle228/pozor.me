import math
import re
from collections.abc import Sequence


def normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def match_query(text: str) -> str:
    # Version 1 dictionary. Raw MATCH syntax is never passed to SQLite.
    synonyms = {"reddit": "реддит", "реддит": "reddit", "game": "игра", "игра": "game"}
    terms: list[str] = []
    stop = {'что', 'как', 'кто', 'где', 'когда', 'почему', 'зачем', 'о', 'об', 'в', 'на', 'из', 'за', 'к', 'по', 'и', 'а', 'но', 'у', 'он', 'она', 'его', 'ее', 'мне', 'меня', 'ты', 'тебе', 'тебя', 'это', 'том', 'то', 'же', 'бы', 'ли', 'ну', 'бот', 'прожарь', 'прожарка', 'думаешь', 'думаете', 'скажи', 'расскажи', 'участник', 'пожалуйста'}
    stop.update({'просто', 'все', 'всё', 'очень', 'тоже', 'только', 'вообще', 'опять', 'ещё', 'еще'})
    for word in normalize(text).split():
        if word in stop:
            continue
        for term in (word, synonyms.get(word, word)):
            if term not in terms and len(terms) < 12:
                terms.append(term)
    return " OR ".join('"' + term + '"' + ('*' if re.fullmatch(r'[а-я]{5,}', term) else '') for term in terms)


def lexical_relevant(text: str, concepts: list[set[str]], *, minimum: int = 2) -> bool:
    """Count independent query concepts, including Russian prefix matches.

    A lone meaningful term is accepted only for a one-concept query. Corpus
    rarity is checked by the repository before admitting that exception.
    """
    words = normalize(text).split()
    matches = sum(any(word == term or (len(term) >= 5 and word.startswith(term))
                      for term in concept for word in words) for concept in concepts)
    return matches >= minimum


def fuse(rankings: Sequence[tuple[float, Sequence[str]]], k: int = 60) -> list[str]:
    if k < 1 or any(not math.isfinite(weight) or weight < 0 for weight, _ in rankings):
        raise ValueError("invalid RRF parameters")
    scores: dict[str, float] = {}
    for weight, ids in rankings:
        for rank, eid in enumerate(dict.fromkeys(ids), 1):
            scores[eid] = scores.get(eid, 0) + weight / (k + rank)
    return sorted(scores, key=lambda eid: (-scores[eid], eid))


