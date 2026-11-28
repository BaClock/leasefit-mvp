"""Read commercial rentals from the Metrapi index and prepare catalog fields."""

import json
import re
from decimal import Decimal, InvalidOperation
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from django.utils.dateparse import parse_datetime

API_ROOT = "https://api.metrapi.ru/v1"
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
POWER_IN_DESCRIPTION = re.compile(
    r"(?:^|[\n;.!?])\s*(?:[•·*⚡–—-]\s*)?"
    r"(?:выделенная(?: электрическая)? мощность|электрическая мощность|электромощность"
    r"|мощность(?: электричества)?|электричество|электроснабжение|выделено(?: на помещение)?)"
    r"\s*(?:[:=–—-]\s*)?([0-9]{1,5}(?:[.,][0-9]{1,2})?)\s*квт"
    r"[^\S\n]*(?=$|[\n;.!?](?![0-9]))",
    re.IGNORECASE,
)
CYRILLIC_LOOKALIKES = str.maketrans("abcekmoptxy", "авсекмортху")
PURPOSES = {
    "office": "office",
    "retail": "retail",
    "catering": "food",
    "warehouse": "warehouse",
    "free_purpose": "other",
}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SHORT_RETRY_SECONDS = 3


class MetrapiError(Exception):
    pass


class ListingNotFound(MetrapiError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def search_rentals(*, locality, query="", area_min=None, area_max=None, limit=10):
    params = {
        "locality": locality,
        "realty_type": "commercial",
        "deal_type": "rent",
        "limit": limit,
    }
    if query:
        params["q"] = query
    for name, value in (("area_min", area_min), ("area_max", area_max)):
        if value is not None:
            params[name] = value
    payload = get_payload(f"/items?{urlencode(params)}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise MetrapiError("Metrapi вернул ответ без списка объявлений.")
    return [preview_item(item) for item in items[:limit] if is_commercial_rental(item)]


def is_commercial_rental(item):
    return (
        isinstance(item, dict)
        and item.get("realty_type") == "commercial"
        and item.get("deal_type") == "rent"
        and not item.get("delisted_at")
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
    error = (
        "Metrapi не нашёл это объявление. Проверьте ссылку или воспользуйтесь поиском."
    )
    try:
        item = get_payload(f"/item/{source}/{source_id}").get("item")
    except ListingNotFound:
        raise MetrapiError(error) from None
    if item is None:
        raise MetrapiError(error)
    if not is_commercial_rental(item):
        raise MetrapiError(
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
    req = Request(
        f"{API_ROOT}{path}",
        headers={"X-Api-Key": settings.METRAPI_API_KEY, "Accept": "application/json"},
    )
    # The destination is fixed; never forward the key across redirects.
    opener = build_opener(NoRedirect())
    try:
        try:
            payload = read_payload(opener, req)
        except HTTPError as exc:
            delay = short_retry_delay(exc)
            if delay is None:
                raise
            sleep(delay)
            payload = read_payload(opener, req)
    except HTTPError as exc:
        if exc.code == 404 and path.startswith("/item/"):
            raise ListingNotFound from None
        if exc.code in {401, 403}:
            raise MetrapiError("Metrapi отклонил ключ или доступ к API.") from None
        if exc.code == 429:
            raise MetrapiError(
                "Достигнут лимит запросов Metrapi. Повторите позже."
            ) from None
        raise MetrapiError(f"Metrapi вернул ошибку HTTP {exc.code}.") from None
    except (URLError, OSError, ValueError):
        raise MetrapiError(
            "Не удалось получить данные Metrapi. Повторите позже."
        ) from None

    if not isinstance(payload, dict):
        raise MetrapiError("Metrapi вернул неожиданный формат ответа.")
    return payload


def read_payload(opener, request):
    with opener.open(request, timeout=10) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise MetrapiError("Ответ Metrapi слишком большой. Уменьшите размер выборки.")
    return json.loads(data, parse_float=Decimal)


def short_retry_delay(error):
    if error.code != 429 or error.fp is None:
        return None
    try:
        with error:
            payload = json.loads(error.read(2048))
    except (OSError, ValueError):
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


def preview_item(item):
    unit = to_text(item.get("price_unit")).lower()
    period = to_text(item.get("price_period")).lower()
    rent = rate = None
    if unit in {"", "за м²"} and period in {"в месяц", "в год", ""}:
        if not unit:
            rent = decimal_value(item.get("price"), 12 if period == "в год" else 1)
        elif period:
            rate = decimal_value(item.get("price"), 12 if period == "в год" else 1)

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

    area = decimal_value(item.get("area_total"))
    floor = to_text(item.get("floor"), 40)
    facts = {}
    if area and Decimal(area) > 0:
        facts["area_sqm"] = {"value": area}
    if floor:
        facts["floor"] = {"text": floor}
    height = decimal_value(item.get("ceiling_height"))
    if height and Decimal(height) > 0:
        facts["ceiling_height_m"] = {"value": height}
    entrance = to_text(item.get("entrance_type"))
    if entrance.casefold() in {"отдельный", "общий"}:
        facts["separate_entrance"] = {"boolean": entrance.casefold() == "отдельный"}

    description = item.get("description")
    power = None
    if isinstance(description, str):
        matches = {
            decimal_value(match[1].replace(",", "."))
            for match in POWER_IN_DESCRIPTION.finditer(
                description.casefold().translate(CYRILLIC_LOOKALIKES)
            )
        }
        if len(matches) == 1:
            power = matches.pop()
            if power is not None:
                facts["power_kw"] = {"value": power}

    tax_form = to_text(item.get("tax_form"))
    vat_mode = {"ндс включён": "included", "ндс не включён": "excluded"}.get(
        tax_form.casefold().replace("ё", "ё").replace("включен", "включён"), "unknown"
    )
    return {
        "title": title or address or "Объявление",
        "label": address or title,
        "city": to_text(item.get("locality"), 120),
        "district": to_text(item.get("district"), 120),
        "source": source,
        "source_label": SOURCES.get(source, source),
        "source_id": source_id,
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
        "entrance": entrance,
        "power": power,
        "facts": facts,
        "lease_type": to_text(item.get("lease_type")),
        "heating": to_text(item.get("heating")),
        "parking": to_text(item.get("parking")),
        "commercial_layout": to_text(item.get("commercial_layout")),
        "commercial_building_type": to_text(item.get("commercial_building_type")),
    }
