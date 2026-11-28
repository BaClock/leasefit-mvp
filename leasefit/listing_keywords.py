"""Recognize explicit premises facts in a listing's Russian description."""

import re
from decimal import Decimal

EXTRACTION_VERSION = "2"

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
    "separate_entrance": (r"(?:отдельн(?:ый|ого)|независим(?:ый|ого)|собственн(?:ый|ого))\s+вход(?:а)?", ""),
    "display_windows": (r"витринны[ех]\s+ок(?:на|он)|витражн(?:ые|ых)\s+ок(?:на|он)", ""),
    "air_conditioning": (r"кондиционировани[ея]|кондиционер(?:а|ы|ов)?", ""),
    "three_phase": (
        r"трехфазн(?:ое|ого)\s+(?:питание|питания|электроснабжение|электроснабжения)|(?:три|3)\s+фазы",
        "",
    ),
    "wet_points": (r"дополнительн(?:ые|ых)\s+мокры[ех]\s+точ(?:ки|ек)", ""),
    "loading_access": (r"зон[аы]\s+разгрузки|разгрузк[аи]\s+товара", ""),
    "access_24h": (r"круглосуточный\s+доступ|доступ\s+24/7", ""),
    "redevelopment": (r"возможность\s+перепланировки|перепланировка\s+разрешена", ""),
    "storage_zone": (r"складская\s+зона|подсобное\s+помещение|зон[аы]\s+хранения", ""),
    "first_line": (r"первая\s+линия(?:\s+улицы)?", ""),
    "power_upgrade": (r"(?:возможность\s+увеличения|возможно\s+увеличение|возможно\s+увеличить)\s+(?:электрическ(?:ой|ую)\s+)?мощност[ьи](?:\s+до\s+[\d,.]+\s*квт)?|возможно\s+увеличение\s+до\s+[\d,.]+\s*квт", ""),
    "hot_water": (r"гвс|горяч[аяей]+\s+вод[аы]", ""),
    "restroom": (r"(?:(?:свой|собственный|отдельный)\s+)?санузел|санузла|туалет[а]?", ""),
    "gas_supply": (r"газ[а]?|газоснабжени[ея]", ""),
    "truck_access": (r"подъезд\s+грузовик(?:ов|а)|подъезд\s+грузового\s+транспорта", ""),
    "service_entrance": (r"служебн(?:ый|ого)\s+вход[а]?|отдельный\s+вход\s+для\s+персонала", ""),
    "signage": (r"возможность\s+размещения\s+вывески|размещение\s+вывески\s+разрешено", ""),
    "extra_exit": (r"(?:второй|дополнительный)\s+эвакуационный\s+выход", ""),
    "accessible_entrance": (r"пандус[а]?|безбарьерный\s+вход", ""),
    "loading_docks": (r"доки|доков|доковые\s+ворота", ""),
    "semi_trailer_access": (r"подъезд\s+фур[ы]?|подъезд\s+большегрузного\s+транспорта", ""),
    "overhead_crane": (r"кран[- ]балк[аи]|крановое\s+оборудование", ""),
    "cold_storage": (r"холодильн(?:ая|ой)\s+камер[аы]|холодильное\s+оборудование", ""),
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


def boolean_evidence(description):
    clauses = {
        re.sub(r"^[^\w]+", "", clause.casefold().replace("ё", "е")).strip(" :"): clause.strip()
        for clause in CLAUSE_SPLIT.split(description)
    }
    for clause, fragment in list(clauses.items()):
        missing = re.match(r"^(?:нет|без)\s+", clause)
        statement = clause[missing.end():] if missing else re.sub(rf"^{POSITIVE}\s+", "", clause)
        statement = re.sub(rf"\s+{POSITIVE}$", "", statement)
        parts = statement.split(" и ")
        if len(parts) > 1 and all(
            any(noun.fullmatch(part) for noun in BARE_NOUNS) for part in parts
        ):
            clauses.update({f"нет {part}" if missing else part: fragment for part in parts})
    results = {}
    for code, (negative, positive) in BOOLEAN_RULES.items():
        known = {
            bool(positive.fullmatch(clause)): fragment for clause, fragment in clauses.items()
            if positive.fullmatch(clause) or negative.fullmatch(clause)
        }
        if code == "separate_entrance":
            for clause, fragment in clauses.items():
                if re.fullmatch(r"(?:общий|совместный)\s+вход|вход\s*:?\s*общий", clause):
                    known[False] = fragment
        if known:
            results[code] = known
    return results


def boolean_facts(description):
    return {code: next(iter(values)) for code, values in boolean_evidence(description).items() if len(values) == 1}


NUMERIC_EXTRA = {
    "floor_load": (r"(?:нагрузка\s+на\s+пол\s*[:–—-]?\s*)?", r"(?:т|кг)\s*/\s*м[²2]"),
    "gate_height": (r"(?:высота\s+ворот|ворота\s+высотой)\s*[:–—-]?\s*", r"(?:м|см)"),
    "gate_width": (r"(?:ширина\s+ворот|ворота\s+шириной)\s*[:–—-]?\s*", r"(?:м|см)"),
    "room_count": (r"", r"(?:кабинет\w*|отдельн\w+\s+помещени\w+)"),
}
ROOM_NUMBERS = {"один": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5}
TEXT_EXTRA = {
    "repair_state": r"без ремонта|без отделки|чистовая отделка|требуется ремонт|косметический ремонт",
    "temperature_mode": r"теплый склад|холодный склад|отапливаемый склад|неотапливаемый склад",
    "noise_limits": r"(?:действуют )?ограничения? (?:по шуму|ночной работы)|шумные работы запрещены",
    "listing_access_hours": r"(?:доступ|режим работы|график доступа)\s+с\s+[0-2]?\d:[0-5]\d\s+до\s+[0-2]?\d:[0-5]\d",
}


def extra_evidence(description):
    results = {}
    for code, (prefix, unit) in NUMERIC_EXTRA.items():
        value_pattern = NUMBER if code != "room_count" else rf"({NUMBER[1:-1]}|{'|'.join(ROOM_NUMBERS)})"
        pattern = re.compile(START + rf"({prefix}{value_pattern}\s*({unit}))" + RANGE_END
                             + r"(?=\s*(?:$|[\n,;.!?]))", re.IGNORECASE)
        values = {}
        for match in pattern.finditer(description):
            raw = match[2].casefold()
            value = Decimal(ROOM_NUMBERS[raw]) if raw in ROOM_NUMBERS else Decimal(raw.replace(",", "."))
            unit_prefix = match[3].casefold()
            if unit_prefix.startswith("кг"):
                value /= 1000
            elif unit_prefix == "см":
                value /= 100
            if value > 0:
                values[format(value, ".2f")] = match[1]
        if values:
            results[code] = ("value", values)
    for code, value_pattern in TEXT_EXTRA.items():
        values = {}
        for clause in CLAUSE_SPLIT.split(description):
            fragment = re.sub(r"^[^\w]+", "", clause).strip()
            if re.fullmatch(value_pattern, fragment.casefold().replace("ё", "е")):
                values[fragment.casefold()] = fragment
        if values:
            results[code] = ("text", values)
    return results
