"""Recognize explicit premises facts in a listing's Russian description."""

import re
from decimal import Decimal


START = r"(?:^|[\n;,.!?])[^\S\n]*(?:[•·*✅✔–—-]\s*)?"
NUMBER = r"([0-9]{1,7}(?:[.,][0-9]{1,2})?)"
RANGE_END = r"(?!\s*(?:[-–—]\s*[0-9]|(?:до|или|и)\s+[0-9]))"
AREA = re.compile(
    START + r"(?:общая\s+)?площадь(?:\s+помещения)?\s*[:=–—-]?\s*"
    + NUMBER + r"\s*(?:м[²2]|кв\.?\s*м|квадратных\s+метров)(?=\W|$)"
    + RANGE_END,
    re.IGNORECASE,
)
FLOOR = re.compile(
    START
    + r"(?:этаж\s*[:=–—]?\s*(-?[0-9]{1,2})(?:\s*[-–]\s*й)?"
    + r"|(-?[0-9]{1,2})(?:\s*-?\s*(?:й|м))?\s+этаж)\b"
    + r"(?=\s*(?:$|[\n,;.!?]))",
    re.IGNORECASE,
)
CLAUSE_SPLIT = re.compile(r"[\n;!?]+|[.,](?!\d)")
FLOOR_WORDS = {
    "первый": "1", "второй": "2", "третий": "3",
    "цокольный": "-1", "подвальный": "-1",
}
TEXT_FLOOR = re.compile(
    START + r"(" + "|".join(FLOOR_WORDS) + r")\s+этаж\b(?=\s*(?:$|[\n,;.!?]))",
    re.IGNORECASE,
)
POSITIVE = r"(?:есть|имеется|имеются|подключен[аоы]?|подведен[аоы]?|установлен[аоы]?|предусмотрен[аоы]?)"
NEGATIVE = r"(?:нет|отсутствует|отсутствуют|не\s+(?:подключен[аоы]?|подведен[аоы]?|предусмотрен[аоы]?))"

# Match complete statements, so a facility mentioned as a possibility stays unknown.
FACILITIES = {
    "water_supply": (
        r"(?:водоснабжение|водоснабжения|вод[аы]|холодная\s+вода|горячая\s+вода)",
        r"центрально[еая]|холодная|горячая",
    ),
    "sewerage": (r"канализаци[яиб]", r"центральная|септик"),
    "ventilation": (
        r"(?:(?:приточная|приточно[- ]вытяжная|естественная|механическая)\s+)?вентиляци[яи]",
        r"приточная|приточно[- ]вытяжная|естественная|механическая",
    ),
    "supply_ventilation": (r"приточн(?:ая|ой|о[- ]вытяжная)\s+вентиляци[яи]", ""),
    "separate_exhaust": (r"(?:отдельная|отдельной|кухонная|кухонной)\s+вытяжк[аи]", ""),
    "heating": (r"отоплени[ея]", r"центральное|автономное|газовое|электрическое"),
    "parking": (r"парковк[аи]", r"бесплатная|платная|своя|собственная"),
    "separate_entrance": (r"отдельн(?:ый|ого)\s+вход(?:а)?", ""),
    "display_windows": (r"витринны[ех]\s+ок(?:на|он)|витражн(?:ые|ых)\s+ок(?:на|он)", ""),
    "air_conditioning": (r"кондиционировани[ея]|кондиционер(?:а|ы|ов)?", ""),
    "three_phase": (
        r"трехфазн(?:ое|ого)\s+(?:питание|питания|электроснабжение|электроснабжения)|(?:три|3)\s+фазы",
        "",
    ),
    "wet_points": (r"дополнительн(?:ые|ых)\s+мокры[ех]\s+точ(?:ки|ек)", ""),
    "loading_access": (r"зон[аы]\s+разгрузки|разгрузк[аи]\s+товара", ""),
    "access_24h": (r"круглосуточный\s+доступ|доступ\s+24/7", ""),
}


def single_area(description):
    values = {Decimal(match[1].replace(",", ".")) for match in AREA.finditer(description)}
    if len(values) == 1 and (value := values.pop()) > 0:
        return format(value, ".2f")
    return None


def single_floor(description):
    values = {str(int(match[1] or match[2])) for match in FLOOR.finditer(description)}
    values.update(FLOOR_WORDS[match[1].casefold()] for match in TEXT_FLOOR.finditer(description))
    return values.pop() if len(values) == 1 else ""


def facility_rules(noun, quality):
    suffix = rf"(?:{POSITIVE}|да|в\s+помещении{f'|{quality}' if quality else ''})"
    prefix = rf"(?:{POSITIVE}{f'|{quality}' if quality else ''})"
    return (
        re.compile(rf"(?:без\s+|нет\s+)(?:{noun})|(?:{noun})\s*[:=–—-]?\s*{NEGATIVE}"),
        re.compile(
            rf"(?:{noun})(?:\s*[:=–—-]?\s*{suffix})?|{prefix}\s+(?:{noun})"
        ),
    )


BOOLEAN_RULES = {code: facility_rules(*rule) for code, rule in FACILITIES.items()}
BARE_NOUNS = [re.compile(noun) for noun, _ in FACILITIES.values()]


def boolean_facts(description):
    clauses = [
        re.sub(r"^[^\w]+", "", clause).strip(" :")
        for clause in CLAUSE_SPLIT.split(description.casefold().replace("ё", "е"))
    ]
    for clause in list(clauses):
        missing = re.match(r"^(?:нет|без)\s+", clause)
        statement = clause[missing.end():] if missing else re.sub(rf"^{POSITIVE}\s+", "", clause)
        parts = statement.split(" и ")
        if len(parts) > 1 and all(
            any(noun.fullmatch(part) for noun in BARE_NOUNS) for part in parts
        ):
            clauses.extend(f"нет {part}" if missing else part for part in parts)
    results = {}
    for code, (negative, positive) in BOOLEAN_RULES.items():
        known = {
            bool(positive.fullmatch(clause)) for clause in clauses
            if positive.fullmatch(clause) or negative.fullmatch(clause)
        }
        if code == "separate_entrance" and any(
            re.fullmatch(r"(?:общий|совместный)\s+вход|вход\s*:?\s*общий", clause)
            for clause in clauses
        ):
            known.add(False)
        if len(known) == 1:
            results[code] = known.pop()
    return results
