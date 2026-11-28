"""Read-only, agency-scoped shortlist preview."""

from collections import Counter
from decimal import Decimal

from datetime import timedelta

from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from .listing_filters import CATEGORY_NAMES

from .geography import district_key, districts_for_city, place_key, requested_districts
from .models import Characteristic, ListingPublication, RequestPremises, Requirement
from .outcomes import GROUPS, budget_excess, outcome_priority
from .services import (
    assess_premises,
    assess_requirements,
    ranking_observations,
    score_order,
)


def power_summary(observations):
    readings = [
        row
        for row in observations
        if row.characteristic.code == "power_kw"
        and row.scope == "premises"
        and not row.superseded
        and row.source_type != "archive"
    ]
    values = {row.value for row in readings if row.value is not None}
    if len(values) != 1 or any(row.status == "conflict" for row in readings):
        return None
    confirmed = any(
        row.value is not None
        and row.status == "confirmed"
        and row.confirmation
        and row.source_type != "listing"
        for row in readings
    )
    return {"value": values.pop(), "confirmed": confirmed}


def for_request_city(catalog, req):
    catalog = catalog.filter(agency_id=req.agency_id)
    if req.city.strip():
        catalog = catalog.filter(
            Q(city__iexact=req.city.strip()) | Q(location_confirmed=False) | Q(city="")
        )
    return catalog


def with_availability(catalog):
    publications = ListingPublication.objects.filter(premises_id=OuterRef("pk"))
    return catalog.annotate(
        has_publications=Exists(publications),
        has_active_publication=Exists(publications.filter(active=True)),
    )


def staff_availability():
    return Q(availability_confirmed_on__gte=timezone.localdate() - timedelta(days=7))


def is_available(obj):
    return not obj.has_publications or obj.has_active_publication or bool(
        obj.availability_confirmed_on and obj.availability_confirmed_on >= timezone.localdate() - timedelta(days=7)
    )


def category_rank(req, obj):
    category, _, format_name = req.business_format.partition(".")
    preferred = {
        "food": {"catering", "retail", "free_purpose"},
        "pickup": {"retail", "free_purpose", "warehouse"},
        "retail": {"retail", "free_purpose"},
        "service": {"retail", "free_purpose"},
        "services": {"retail", "free_purpose"},
        "office": {"office", "free_purpose"},
        "production": {"manufacturing", "warehouse"},
    }.get(category, set())
    if "bak" in format_name:
        preferred = preferred | {"manufacturing"}
    listing = getattr(obj, "listing", None)
    categories = set(listing.categories if listing else ())
    return not bool(preferred.intersection(categories))


def top_order(card):
    return (
        card["unclear_count"], score_order(card), Decimal(card["economics"]["monthly_total"]),
        card["category_rank"], card["p"].pk,
    )


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


def recommend_catalog_premises(
    req, catalog, requirements, filters, selected_cards, *, district_names=None
):
    """Evaluate all eligible candidates before sorting or paginating."""
    counts = Counter({group: 0 for group in GROUPS})
    if not any(row.priority == "required" for row in requirements):
        return [], counts
    candidates = with_availability(for_request_city(catalog, req)).select_related("listing")
    city_key = place_key(req.city)
    registered = districts_for_city(req.city)
    known_districts = {district_key(name) for name in registered}
    if district_names is None:
        available = ()
        if not registered and req.geography.strip():
            available = (
                candidates.exclude(district="")
                .values_list("district", flat=True)
                .distinct()
            )
        district_names, _ = requested_districts(req, available)
    required_districts = {district_key(name) for name in district_names or ()}
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
        candidates = candidates.exclude(pk__in=selected_cards)
    if filters.get("availability") == "archived":
        candidates = candidates.filter(has_publications=True, has_active_publication=False).exclude(staff_availability())
    else:
        candidates = candidates.filter(Q(has_publications=False) | Q(has_active_publication=True) | staff_availability())
    criteria = technical_filters(filters)
    candidates = candidates.prefetch_related(
        Prefetch(
            "observations",
            queryset=ranking_observations(
                [*requirements, *criteria], extra_codes=("power_kw",)
            ),
            to_attr="ranking_observations",
        )
    )

    results = []
    contexts = {
        link.premises_id: link for link in RequestPremises.objects.filter(request=req, premises_id__in=selected_cards)
    }
    for obj in candidates:
        district = district_key(obj.district)
        preliminary = obj.has_active_publication
        listing_location = preliminary and place_key(obj.city) == city_key and district in known_districts and not any(
            row["key"] in {"city", "district", "label"} for row in obj.listing_conflicts
        )
        if preliminary and obj.city and city_key and place_key(obj.city) != city_key:
            continue
        location_uncertain = bool(registered) and (
            district not in known_districts
            or not (obj.location_confirmed or listing_location)
            or place_key(obj.city) != city_key
        )
        if (
            district_names is not None
            and district not in required_districts
            and not location_uncertain
        ):
            continue
        observations = obj.ranking_observations
        if criteria and assess_requirements(obj, criteria, observations, preliminary=preliminary)[0] != "pass":
            continue
        card = (
            dict(selected_cards[obj.pk])
            if obj.pk in selected_cards and not obj.has_publications
            else assess_premises(req, obj, requirements, observations, context=contexts.get(obj.pk), preliminary=preliminary)
        )
        card["p"] = obj
        costs = card["economics"]
        if filters.get("monthly_max") is not None and (
            not costs["complete"]
            or Decimal(costs["monthly_total"]) > filters["monthly_max"]
        ):
            continue
        if filters.get("only_confirmed") and not card["evidence"]["confirmed"]:
            continue
        available = is_available(obj)
        card["catalog_group"] = "archived" if not available else "location" if location_uncertain else card["outcome"]["group"]
        counts[card["catalog_group"]] += 1
        if filters.get("status") and card["status"] != filters["status"]:
            continue
        if filters.get("budget_state") and card["outcome"]["monthly"]["state"] != filters["budget_state"]:
            continue
        if filters.get("score_min") is not None and (card["score"] is None or card["score"] < filters["score_min"]):
            continue
        if filters.get("completeness") == "complete" and (card["unclear_count"] or not costs["complete"]):
            continue
        if filters.get("completeness") == "unknown" and not (card["unclear_count"] or not costs["complete"]):
            continue
        card["selected"] = obj.pk in selected_cards
        card["location_by_listing"] = listing_location and not obj.location_confirmed
        card["power"] = power_summary(observations)
        card["category_rank"] = category_rank(req, obj)
        listing = getattr(obj, "listing", None)
        card["categories"] = [CATEGORY_NAMES[code] for code in (listing.categories if listing else ()) if code in CATEGORY_NAMES]
        results.append(card)
    results.sort(key=lambda card: sort_key(card, filters.get("sort")))
    return results, counts


def sort_key(card, mode):
    obj = card["p"]
    status = ((99, 99), ()) if card["catalog_group"] == "archived" else (outcome_priority(card), budget_excess(card))
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
