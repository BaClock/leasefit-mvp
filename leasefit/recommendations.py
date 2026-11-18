"""Read-only, agency-scoped shortlist preview."""

from collections import Counter
from decimal import Decimal

from django.db.models import Prefetch, Q

from .models import Characteristic, Requirement
from .services import (
    STATUS_ORDER,
    assess_premises,
    assess_requirements,
    ranking_observations,
    score_order,
)


def for_request_city(catalog, req):
    catalog = catalog.filter(agency_id=req.agency_id)
    if req.city.strip():
        catalog = catalog.filter(
            Q(city__iexact=req.city.strip()) | Q(location_confirmed=False) | Q(city="")
        )
    return catalog


def technical_filters(filters):
    selected = {
        code: filters[field]
        for field, code in (
            ("power_min", "power_kw"),
            ("water", "water_supply"),
            ("ventilation", "ventilation"),
        )
        if filters.get(field) not in (None, "")
    }
    if not selected:
        return []
    characteristics = Characteristic.objects.in_bulk(selected, field_name="code")
    return [
        Requirement(
            characteristic=characteristics[code],
            name=characteristics[code].label,
            unit=characteristics[code].unit,
            operator="gte" if code == "power_kw" else "eq",
            minimum=value if code == "power_kw" else None,
            expected=value == "yes",
            priority="required",
        )
        for code, value in selected.items()
    ]


def recommend_catalog_premises(req, catalog, requirements, filters, selected_ids):
    """Evaluate all eligible candidates before sorting or paginating."""
    counts = Counter({status: 0 for status in STATUS_ORDER})
    if not any(row.priority == "required" for row in requirements):
        return [], counts
    candidates = for_request_city(catalog, req)
    for field, lookup in (
        ("q", "label__icontains"),
        ("premises_type", "premises_type"),
        ("area_min", "area__gte"),
        ("area_max", "area__lte"),
        ("district", "district"),
    ):
        value = filters.get(field)
        if value not in (None, ""):
            candidates = candidates.filter(**{lookup: value})
    if filters.get("only_new"):
        candidates = candidates.exclude(pk__in=selected_ids)
    criteria = technical_filters(filters)
    candidates = candidates.prefetch_related(
        Prefetch(
            "observations",
            queryset=ranking_observations([*requirements, *criteria]),
            to_attr="ranking_observations",
        )
    )

    results = []
    for obj in candidates:
        observations = obj.ranking_observations
        if criteria and assess_requirements(obj, criteria, observations)[0] != "pass":
            continue
        card = assess_premises(req, obj, requirements, observations, context=None)
        costs = card["economics"]
        if filters.get("monthly_max") is not None and (
            not costs["complete"]
            or Decimal(costs["monthly_total"]) > filters["monthly_max"]
        ):
            continue
        if filters.get("only_confirmed") and not card["evidence"]["confirmed"]:
            continue
        counts[card["status"]] += 1
        if filters.get("status") and card["status"] != filters["status"]:
            continue
        card["selected"] = obj.pk in selected_ids
        card["matched"] = [row for row in card["rows"] if row["status"] == "pass"]
        card["unclear"] = [row for row in card["rows"] if row["status"] == "clarify"]
        card["blockers"] = [row for row in card["rows"] if row["status"] == "blocker"]
        card["unmet_preferences"] = [
            row for row in card["rows"] if row["status"] == "desired_fail"
        ]
        card["has_monthly_values"] = any(
            value is not None for _, value in costs["monthly"][:-1]
        )
        results.append(card)
    results.sort(key=lambda card: sort_key(card, filters.get("sort")))
    return results, counts


def sort_key(card, mode):
    obj = card["p"]
    status = STATUS_ORDER[card["status"]]
    score = score_order(card)
    if mode == "monthly":
        costs = card["economics"]
        return (
            status,
            not costs["complete"],
            Decimal(costs["monthly_total"]),
            score,
            obj.pk,
        )
    if mode == "area":
        return status, obj.area is None, -(obj.area or 0), score, obj.pk
    if mode == "score":
        return status, score, obj.pk
    return status, score, -card["evidence"]["confirmed"], obj.pk
