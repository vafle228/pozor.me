"""Conservative deterministic guard; model judgement adds another independent check.

This lexicon is deliberately reviewable. It cannot prove semantic safety, so
human evaluation remains part of acceptance for Russian-language humour.
"""

import re

PATTERNS = (
    r"(?:убью|убить|убей|зареж|расстрел|изнасил|сдохни|суицид|самоубий)",
    r"(?:рак\b|онколог|шизофрен|психопат|диагноз|инвалид|аутист|даун\b)",
    r"(?:нацист|ниггер|чурк|жид\b|пидор|педик|хач\b)",
    r"(?:педофил|несовершеннолет|изменяешь|наркоман|алкоголик|воруешь|преступник)",
    r"(?:секс|порн|проститут|аборт|похорон|умерла|умерли|мертв.*(?:мать|отец|родител))",
    r"(?:твой|твоя|твои|ты).{0,25}(?:религи|националь|ориентаци|рас[аы]\b)",
    r"(?:живёшь|живешь|адрес|работаешь)\s+(?:на|в|по)\s+",
)


def redline(text: str, level: int = 2) -> str | None:
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in PATTERNS):
        return "redline"
    if level == 1 and re.search(r"(?:бля|хуй|хуе|пизд|еб[ауё]|ёб)", text, re.IGNORECASE):
        return "level_language"
    return None
