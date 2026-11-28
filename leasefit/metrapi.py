"""Read commercial rentals from the Metrapi index and prepare catalog fields."""

import json
import re
from decimal import Decimal, InvalidOperation
from http.client import HTTPException, HTTPSConnection
from time import sleep
from urllib.parse import unquote, urlencode, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .characteristics import BY_CODE
from .listing_keywords import (
    AREA, FLOOR, TEXT_FLOOR, EXTRACTION_VERSION, FACILITIES, boolean_evidence,
    extra_evidence, single_floor,
)
from .listing_filters import CATEGORY_NAMES
from .metrapi_limits import QuotaExceeded, pause_for_quota, reserve_request

API_HOST = "api.metrapi.ru"
SOURCES = {
    "avito": "Авито",
    "cian": "Циан",
    "domclick": "Домклик",
    "yandex": "Яндекс Недвижимость",
    "farpost": "FarPost",
    "move": "Move.ru",
    "sob": "sob.ru",
    "youla": "Юла",
}
LISTING_LINKS = {
    "avito.ru": ("avito", r"(?:^|_)([0-9]{1,24})$"),
    "cian.ru": ("cian", r"^([0-9]{1,24})$"),
    "domclick.ru": ("domclick", r"(?:^|__)([0-9]{1,24})$"),
    "realty.yandex.ru": ("yandex", r"^([0-9]{1,24})$"),
}
PHOTO_DOMAINS = {
    "avito": ("img.avito.st",),
    "cian": ("cdn-cian.ru", "cian.ru"),
    "domclick": ("img.dmclk.ru", "domclick.ru"),
    "yandex": ("avatars.mds.yandex.net",),
    "farpost": ("farpost.ru",),
    "move": ("move.ru",),
    "sob": ("sob.ru",),
    "youla": ("youla.ru",),
}
CEILING_HEIGHT_CODE = "ceiling_height_m"
CEILING_IN_DESCRIPTION = re.compile(
    r"(?:^|[\n;,.!?])\s*(?:[•·*–—-]\s*)?"
    r"(?:высота\s+потолк(?:а|ов)|потолки(?:\s+высотой)?)\s*"
    r"(?:[:=–—-]\s*|составляет\s+)?"
    r"([0-9]{1,2}(?:[.,][0-9]{1,2})?)\s*(?:метр(?:а|ов)?|м)\b"
    r"(?!\.?\s*(?:[-–—]\s*[0-9]|(?:до|или|и)\s+[0-9]|[0-9]+\s*см))",
    re.IGNORECASE,
)
POWER_IN_DESCRIPTION = re.compile(
    r"(?:^|[\n;,!?]|\.(?![0-9]))\s*(?:[•·*⚡–—-]\s*)?"
    r"(?:выделенная(?: электрическая)? мощность|электрическая мощность|электромощность"
    r"|мощность(?: электричества)?|электричество|электроснабжение|выделено(?: на помещение)?)"
    r"\s*(?:[:=–—-]\s*)?([0-9]{1,5}(?:[.,][0-9]{1,2})?)\s*квт"
    r"[^\S\n]*(?=$|[\n;,.!?](?![0-9]))",
    re.IGNORECASE,
)
CYRILLIC_LOOKALIKES = str.maketrans("abcekmoptxy", "авсекмортху")
PURPOSES = {
    "office": "office",
    "retail": "retail",
    "catering": "food",
    "warehouse": "warehouse",
    "manufacturing": "other",
    "free_purpose": "other",
}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SEARCH_PAGE_SIZE = 50
MAX_SHORT_RETRY_SECONDS = 3
MAX_LISTING_DESCRIPTION = 32000
MAX_LISTING_PHOTOS = 80
CONTACT_IN_DESCRIPTION = re.compile(
    r"(?<!\w)(?:(?:\+7|8)[ \t().-]*(?:\d[ \t().-]*){9}\d(?!\d)"
    r"|[\w.+-]+@[\w.-]+\.[a-zа-я]{2,})(?!\w)",
    re.IGNORECASE,
)
BOOLEAN_FIELDS = {
    "separate_entrance": ("entrance_type", {"отдельный"}, {"общий"}),
    "water_supply": ("water_supply", {"центральное", "скважина", "колодец"}, set()),
    "sewerage": ("sewerage", {"центральная", "септик", "выгребная яма"}, set()),
    "heating": (
        "heating",
        {"центральное", "автономное", "газовое", "электрическое", "твердотопливное", "печь или камин"},
        set(),
    ),
    "parking": (
        "parking", {"открытая", "закрытая", "наземная", "подземная", "многоуровневая", "бесплатная", "платная"},
        set(),
    ),
}


class MetrapiError(Exception):
    pass


class ListingNotFound(MetrapiError):
    pass


class ListingUnavailable(MetrapiError):
    pass


class CursorExpired(MetrapiError):
    pass


class MetrapiQuota(MetrapiError):
    pass


def rental_page(params, *, cursor=None, since=None, feed=False, limit=SEARCH_PAGE_SIZE, include_inactive=False):
    params = {**params, "limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    elif since is not None:
        params["since"] = since
    if include_inactive:
        params["archived"] = 1
    endpoint = "feed" if feed else "items"
    payload = get_payload(f"/{endpoint}?{urlencode(params, doseq=True)}")
    items, next_cursor = payload.get("items"), payload.get("next_cursor")
    if not isinstance(items, list) or (
        next_cursor is not None and (
            isinstance(next_cursor, bool) or not isinstance(next_cursor, (str, int)) or next_cursor == ""
        )
    ) or (feed and next_cursor is None) or (not feed and next_cursor is not None and cursor == next_cursor):
        raise MetrapiError("Metrapi вернул некорректную страницу объявлений.")
    return [
        preview_item(item) for item in items
        if is_commercial_rental(item, include_inactive=include_inactive)
    ], next_cursor, len(items)


def search_rentals(*, locality, query="", area_min=None, area_max=None, cursor=None):
    params = {
        "locality": locality,
        "realty_type": "commercial",
        "deal_type": "rent",
        "limit": SEARCH_PAGE_SIZE,
    }
    if cursor is not None:
        params["cursor"] = cursor
    if query:
        params["q"] = query
    for name, value in (("area_min", area_min), ("area_max", area_max)):
        if value is not None:
            params[name] = value
    payload = get_payload(f"/items?{urlencode(params)}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise MetrapiError("Metrapi вернул ответ без списка объявлений.")
    next_cursor = payload.get("next_cursor")
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor
    ):
        raise MetrapiError("Metrapi не указал следующую страницу. Повторите запрос.")
    return [
        preview_item(item) for item in items if is_commercial_rental(item)
    ], next_cursor


def is_commercial_rental(item, *, include_inactive=False):
    return (
        isinstance(item, dict)
        and item.get("realty_type") == "commercial"
        and item.get("deal_type") == "rent"
        and (include_inactive or not item.get("delisted_at"))
        and not (
            isinstance(item.get("commercial_types"), list)
            and "land" in item["commercial_types"]
        )
    )


def listing_reference(url):
    """Extract a provider ID from a supported public link without opening it."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
        ):
            raise ValueError
    except ValueError:
        raise MetrapiError("Укажите полную ссылку на объявление.") from None

    host = parts.hostname.lower()
    last_part = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    for domain, (source, pattern) in LISTING_LINKS.items():
        if host == domain or host.endswith("." + domain):
            match = re.search(pattern, last_part)
            if match:
                return source, match[1]
            break
    raise MetrapiError(
        "Нужна прямая ссылка на объявление Авито, Циан, Домклик или Яндекс Недвижимости."
    )


def find_rental(reference):
    source, source_id = reference
    if source not in SOURCES or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
        raise MetrapiError("Не удалось определить площадку или номер объявления.")
    error = (
        "Metrapi не нашёл это объявление. Проверьте ссылку или воспользуйтесь поиском."
    )
    try:
        item = get_payload(f"/item/{source}/{source_id}").get("item")
    except ListingNotFound:
        raise ListingUnavailable(error) from None
    if item is None:
        raise ListingUnavailable(error)
    if not is_commercial_rental(item):
        raise ListingUnavailable(
            "Объявление должно быть действующим предложением коммерческой аренды помещения."
        )
    if item.get("source") != source or to_text(item.get("source_id")) != source_id:
        raise MetrapiError(
            "Metrapi вернул данные другого объявления. Проверьте ссылку."
        )
    return preview_item(item)


def get_payload(path):
    if not settings.METRAPI_API_KEY:
        raise MetrapiError("Для поиска задайте METRAPI_API_KEY на сервере.")
    try:
        reserve_request()
        status, data = read_payload(path)
        if status == 429:
            delay = short_retry_delay(data)
            if delay is not None:
                sleep(delay)
                reserve_request()
                status, data = read_payload(path)
    except QuotaExceeded:
        raise MetrapiQuota("Достигнут лимит запросов Metrapi на текущие сутки UTC.") from None
    except (OSError, HTTPException):
        raise MetrapiError(
            "Не удалось получить данные Metrapi. Повторите позже."
        ) from None

    if status == 404 and path.startswith("/item/"):
        raise ListingNotFound
    if status in {401, 403}:
        raise MetrapiError("Metrapi отклонил ключ или доступ к API.")
    if status == 429:
        if b"daily limit exceeded" in data.lower():
            pause_for_quota()
            raise MetrapiQuota("Достигнут лимит запросов Metrapi на текущие сутки UTC.")
        raise MetrapiError("Достигнут лимит запросов Metrapi. Повторите позже.")
    if status == 410 and path.startswith("/feed"):
        raise CursorExpired("Курсор Metrapi устарел. Требуется повторная загрузка каталога.")
    if status != 200:
        raise MetrapiError(f"Metrapi вернул ошибку HTTP {status}.")
    if len(data) > MAX_RESPONSE_BYTES:
        raise MetrapiError("Ответ Metrapi слишком большой. Уменьшите размер выборки.")
    try:
        payload = json.loads(data, parse_float=Decimal)
    except ValueError:
        raise MetrapiError("Metrapi вернул неожиданный формат ответа.") from None
    if not isinstance(payload, dict):
        raise MetrapiError("Metrapi вернул неожиданный формат ответа.")
    return payload


def read_payload(path):
    # Metrapi response reads can hang when the request sends Connection: close.
    # This client uses the fixed host and closes the socket after reading.
    conn = HTTPSConnection(API_HOST, timeout=10)
    try:
        conn.request(
            "GET",
            f"/v1{path}",
            headers={
                "X-Api-Key": settings.METRAPI_API_KEY,
                "Accept": "application/json",
                "User-Agent": "LeaseFIT",
            },
        )
        with conn.getresponse() as response:
            if response.status == 200:
                return response.status, response.read(MAX_RESPONSE_BYTES + 1)
            if response.status == 429:
                return response.status, response.read(2048)
            return response.status, b""
    finally:
        conn.close()


def short_retry_delay(data):
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and detail.get("reason") == "too_soon":
        seconds = detail.get("retry_after")
        if type(seconds) in (int, float) and 0 <= seconds <= MAX_SHORT_RETRY_SECONDS:
            return seconds + 0.1
    return None


def to_text(value, limit=250):
    return str(value).strip()[:limit] if isinstance(value, (str, int, Decimal)) else ""


def decimal_value(value, divisor=1):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal, float)):
        return None
    try:
        number = Decimal(str(value)) / divisor
        if number.is_finite() and 0 <= number < Decimal("999999999999.995"):
            return format(number.quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError):
        pass
    return None


def ruble_amount(value, free_label):
    label = " ".join(to_text(value).lower().split())
    if label == free_label:
        return "0.00"
    if re.fullmatch(r"[\d ]+(?:[,.]\d{1,2})?\s*(?:₽|руб\.?)", label):
        return decimal_value(
            re.sub(r"\s*(?:₽|руб\.?)$", "", label).replace(" ", "").replace(",", ".")
        )
    return None


def source_date(item):
    today = timezone.localdate()
    for name in ("updated_at", "published_at"):
        try:
            stamp = parse_datetime(to_text(item.get(name), 60))
        except ValueError:
            continue
        if stamp:
            date = (
                timezone.localdate(stamp) if timezone.is_aware(stamp) else stamp.date()
            )
            if date <= today:
                return date.isoformat()
    return today.isoformat()


def photo_urls(source, values):
    if not isinstance(values, list):
        return []
    valid = {}
    domains = PHOTO_DOMAINS.get(source, ())
    for value in values:
        if not isinstance(value, str) or not 0 < len(value) <= 2048:
            continue
        try:
            URLValidator(schemes=["https"])(value)
            parts = urlsplit(value)
            host = parts.hostname
            if (
                parts.username is not None
                or parts.password is not None
                or parts.port is not None
                or not host
                or not any(
                    host == domain or host.endswith("." + domain) for domain in domains
                )
            ):
                continue
        except (ValueError, ValidationError):
            continue
        valid[value] = None
        if len(valid) >= MAX_LISTING_PHOTOS:
            break
    return list(valid)


def ceiling_height_from_description(description):
    matches = {
        decimal_value(match[1].replace(",", "."))
        for match in CEILING_IN_DESCRIPTION.finditer(description)
    }
    if len(matches) != 1:
        return None
    height = matches.pop()
    return height if height is not None and Decimal(height) > 0 else None


def reported_booleans(item):
    facts = {}
    for code in FACILITIES:
        field, positive, negative = BOOLEAN_FIELDS.get(code, (code, set(), set()))
        raw = item.get(field)
        if isinstance(raw, list) and raw:
            variants = {to_text(value).strip().casefold().replace("ё", "е") for value in raw}
            if variants <= positive | {"да", "есть"}:
                facts[code] = {"boolean": True}
            elif variants <= negative | {"нет", "отсутствует"}:
                facts[code] = {"boolean": False}
            continue
        value = to_text(raw).casefold().replace("ё", "е")
        if isinstance(raw, bool):
            facts[code] = {"boolean": raw}
        elif value in positive | {"да", "есть", "1", "true"}:
            facts[code] = {"boolean": True}
        elif value in negative | {"нет", "отсутствует", "0", "false"}:
            facts[code] = {"boolean": False}
    return facts


def merge_description_fact(facts, code, field, readings):
    if not readings:
        return
    fact = facts.setdefault(code, {})
    structured = fact.get(field)
    extracted = next(iter(readings)) if len(readings) == 1 else None
    if structured is None and extracted is not None:
        fact.update({field: extracted, "locator": "Описание объявления"})
    if len(readings) > 1 or (structured is not None and structured != extracted):
        fact["conflicting"] = True
    fact["extraction"] = {"version": EXTRACTION_VERSION, "fragments": list(readings.values())}


def numeric_evidence(pattern, description):
    return {
        value: description[match.start():match.end()].strip(" ;,.!\n")
        for match in pattern.finditer(description)
        if (value := decimal_value(match[1].replace(",", "."))) is not None
        and Decimal(value) > 0
    }


def preview_item(item):
    unit = to_text(item.get("price_unit")).lower()
    period = to_text(item.get("price_period")).lower()
    rent = rate = None
    if unit in {"", "за м²"} and period in {"в месяц", "в год", ""}:
        if not unit:
            rent = decimal_value(item.get("price"), 12 if period == "в год" else 1)
        elif period:
            rate = decimal_value(item.get("price"), 12 if period == "в год" else 1)
    advertised_period = to_text(item.get("src_price_sqm_period")).casefold()
    if rent is None and rate is None and unit != "за м³" and advertised_period in {"в месяц", "в год"}:
        rate = decimal_value(item.get("src_price_sqm"), 12 if advertised_period == "в год" else 1)

    purposes = item.get("commercial_types")
    purposes = (
        [to_text(purpose, 50) for purpose in purposes]
        if isinstance(purposes, list)
        else []
    )
    kind = next(
        (PURPOSES[p] for p in purposes if p in PURPOSES and p != "free_purpose"), None
    )
    kind = kind or ("other" if purposes else "unknown")
    title = to_text(item.get("title"))
    address = to_text(item.get("address"))
    source = to_text(item.get("source"), 40)
    source_id = to_text(item.get("source_id"), 80)
    url = to_text(item.get("url"), 1500)
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        url = ""

    raw_description = item.get("description")
    description = (
        CONTACT_IN_DESCRIPTION.sub("[контакт скрыт]", raw_description.replace("\x00", ""))[:MAX_LISTING_DESCRIPTION]
        if isinstance(raw_description, str)
        else ""
    )
    facts = reported_booleans(item)
    for code, readings in boolean_evidence(description).items():
        merge_description_fact(facts, code, "boolean", readings)
    if repair := to_text(item.get("renovation")).casefold().replace("ё", "е"):
        facts["repair_state"] = {"text": repair}
    for code, (field, readings) in extra_evidence(description).items():
        merge_description_fact(facts, code, field, readings)
    area = decimal_value(item.get("area_total"))
    if area is not None and Decimal(area) > 0:
        facts["area_sqm"] = {"value": area}
    area_readings = numeric_evidence(AREA, description)
    merge_description_fact(facts, "area_sqm", "value", area_readings)
    area = facts.get("area_sqm", {}).get("value")
    floor = to_text(item.get("floor"), 40)
    if floor:
        facts["floor"] = {"text": floor}
    floor_readings = {}
    for pattern in (FLOOR, TEXT_FLOOR):
        for match in pattern.finditer(description):
            fragment = match[0].strip(" ;,.!\n")
            if number := single_floor(fragment):
                floor_readings[number] = fragment
    merge_description_fact(facts, "floor", "text", floor_readings)
    floor = facts.get("floor", {}).get("text", "")
    height = decimal_value(item.get("ceiling_height"))
    if height is not None and Decimal(height) > 0:
        facts[CEILING_HEIGHT_CODE] = {"value": height}
    heights = numeric_evidence(CEILING_IN_DESCRIPTION, description)
    merge_description_fact(facts, CEILING_HEIGHT_CODE, "value", heights)
    height_fact = facts.get(CEILING_HEIGHT_CODE, {})
    height = height_fact.get("value")
    height_from_description = bool(height_fact.get("locator"))
    entrance = to_text(item.get("entrance_type"))

    power = decimal_value(item.get("power_kw"))
    if power is not None and Decimal(power) > 0:
        facts["power_kw"] = {"value": power}
    powers = {
        value: description[match.start():match.end()].strip(" ;,.!\n")
        for match in POWER_IN_DESCRIPTION.finditer(description.casefold().translate(CYRILLIC_LOOKALIKES))
        if (value := decimal_value(match[1].replace(",", "."))) is not None and Decimal(value) > 0
    }
    merge_description_fact(facts, "power_kw", "value", powers)
    power = facts.get("power_kw", {}).get("value")
    if facts.get("access_24h", {}).get("boolean") is True and not facts["access_24h"].get("conflicting"):
        facts["operating_schedule"] = {
            **{k: v for k, v in facts["access_24h"].items() if k in {"locator", "extraction"}},
            "structured_value": {"mode": "always"},
        }

    description_facts = []
    for code in sorted(facts):
        fact = facts[code]
        if not fact.get("locator") or code in {CEILING_HEIGHT_CODE, "power_kw"}:
            continue
        spec = BY_CODE[code]
        if fact.get("conflicting") or "structured_value" in fact:
            continue
        if "boolean" in fact:
            value = "Есть" if fact["boolean"] else "Нет"
        else:
            value = " ".join(filter(None, (fact.get("value") or fact.get("text"), spec["unit"])))
        description_facts.append((spec["label"], value))

    tax_form = to_text(item.get("tax_form"))
    vat_mode = {"ндс включён": "included", "ндс не включён": "excluded"}.get(
        tax_form.casefold().replace("ё", "ё").replace("включен", "включён"), "unknown"
    )
    return {
        "title": title or address or "Объявление",
        "label": address or title,
        "address": address,
        "categories": sorted(set(purposes).intersection(CATEGORY_NAMES)),
        "active": not item.get("delisted_at"),
        "details": {
            **{
                key: to_text(item.get(key), 500)
                for key in (
                    "address_alt", "street", "house_number", "floors_total", "build_year",
                    "renovation", "building_class", "rent_period", "published_at",
                    "updated_at", "first_seen", "delisted_at", "commercial_layout",
                    "commercial_building_type", "lease_type", "entrance_type",
                    "heating", "parking", "tax_form", "deposit", "commission",
                    "business_center", "prepayment", "utilities_payer", "area_text",
                    "last_seen", "region", "microdistrict", "bathroom", "house_type",
                    "ceiling_type", "build_status", "elevators", "security",
                )
                if item.get(key) not in (None, "", [], {})
            },
            **{key: str(item[key]) for key in ("lat", "lon") if isinstance(item.get(key), (int, Decimal))},
            "commercial_types": sorted(set(purposes)),
        },
        "city": to_text(item.get("locality"), 120),
        "district": to_text(item.get("district"), 120),
        "source": source,
        "source_label": SOURCES.get(source, source),
        "source_id": source_id,
        "group_id": to_text(item.get("group_id"), 120),
        "group_spread_m": decimal_value(item.get("group_spread_m")),
        "url": url,
        "date": source_date(item),
        "price": decimal_value(item.get("price")),
        "price_unit": unit,
        "price_period": period,
        "rent": rent,
        "rate": rate,
        "area": area,
        "floor": floor,
        "premises_type": kind,
        "vat_mode": vat_mode,
        "tax_form": tax_form,
        "deposit_text": to_text(item.get("deposit")),
        "deposit": ruble_amount(item.get("deposit"), "без залога"),
        "commission_text": to_text(item.get("commission")),
        "commission": ruble_amount(item.get("commission"), "без комиссии"),
        "height": height,
        "height_from_description": height_from_description,
        "description": description,
        "photos": photo_urls(source, item.get("photos")),
        "entrance": entrance,
        "power": power,
        "facts": facts,
        "description_facts": description_facts,
        "lease_type": to_text(item.get("lease_type")),
        "heating": to_text(item.get("heating")),
        "parking": to_text(item.get("parking")),
        "commercial_layout": to_text(item.get("commercial_layout")),
        "commercial_building_type": to_text(item.get("commercial_building_type")),
    }
