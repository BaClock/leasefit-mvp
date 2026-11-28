"""Shared filter semantics for scheduled discovery and manual imports."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .geography import district_key, place_key

CATEGORIES = (
    ("warehouse", "Склады"), ("office", "Офисы"), ("retail", "Торговые помещения"),
    ("catering", "Общепит"), ("manufacturing", "Производство"),
    ("free_purpose", "Свободное назначение"),
)
CATEGORY_NAMES = dict(CATEGORIES)


def number(value):
    try:
        result = Decimal(str(value).replace(",", "."))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def within(value, lower, upper):
    low, high = number(lower), number(upper)
    if low is None and high is None:
        return True
    value = number(value)
    return value is not None and (low is None or low <= value) and (high is None or value <= high)


def api_filters(filters, categories):
    params = {"locality": filters.get("locality") or "Новосибирск",
              "realty_type": "commercial", "deal_type": "rent"}
    if categories:
        params["commercial_type"] = categories
    for key in ("source", "area_min", "area_max", "date_from", "date_to"):
        if filters.get(key) not in (None, "", []):
            params[key] = filters[key]
    if filters.get("query"):
        params["q"] = filters["query"]
    return params


def matches(item, filters, categories=()):
    if place_key(item["city"]) != place_key(filters.get("locality") or "Новосибирск"):
        return False
    if categories and not set(categories).intersection(item["categories"]):
        return False
    if filters.get("source") and item["source"] not in filters["source"]:
        return False
    if filters.get("districts") and district_key(item["district"]) not in {
        district_key(name) for name in filters["districts"]
    }:
        return False
    months = number(item["rent"])
    area, rate = number(item["area"]), number(item["rate"])
    if months is None and area and rate is not None:
        months = area * rate
    if rate is None and area and months is not None:
        rate = months / area
    for name, value in (("area", area), ("rent", months), ("rate", rate), ("floor", item["floor"])):
        if not within(value, filters.get(f"{name}_min"), filters.get(f"{name}_max")):
            return False
    text = " ".join((item["title"], item["label"], item["description"])).casefold().replace("ё", "е")
    for key, excluded in (("keywords", False), ("exclude_words", True)):
        terms = [x.strip().casefold().replace("ё", "е") for x in re.split(r"[,\n]+", filters.get(key) or "") if x.strip()]
        if terms and any(term in text for term in terms) == excluded:
            return False
    published = item["details"].get("published_at", "")[:10]
    for key, upper in (("date_from", False), ("date_to", True)):
        if filters.get(key):
            try:
                actual = date.fromisoformat(published)
                bound = date.fromisoformat(str(filters[key]))
            except ValueError:
                return False
            if (actual > bound if upper else actual < bound):
                return False
    return True
