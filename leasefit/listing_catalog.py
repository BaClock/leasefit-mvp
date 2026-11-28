"""Agency-scoped import and versioning of Metrapi publications."""

import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .metrapi import SOURCES
from .metrapi_listings import premises_listing_reference, source_name
from .models import (
    Agency, Characteristic, ListingPublication, ListingPublicationVersion,
    Observation, Premises, PremisesListing,
)

CARD_FIELDS = {
    "city": "Город", "district": "Район", "floor": "Этаж",
    "premises_type": "Тип помещения", "area": "Площадь",
    "rent": "Аренда в месяц", "rate": "Ставка за м² в месяц",
    "deposit": "Залог", "commission": "Комиссия", "vat_mode": "НДС",
}
FINANCE_FIELDS = {"area", "rent", "rate", "deposit", "commission", "vat_mode"}
SNAPSHOT_FIELDS = {
    **CARD_FIELDS, "label": "Адрес", "title": "Заголовок", "url": "Ссылка",
    "description": "Описание", "photos": "Фотографии", "facts": "Характеристики",
    "price": "Цена в объявлении", "price_unit": "Единица цены",
    "price_period": "Период цены", "tax_form": "Условия НДС",
    "deposit_text": "Условия залога", "commission_text": "Условия комиссии",
    "lease_type": "Вид договора", "heating": "Отопление", "parking": "Парковка",
    "commercial_layout": "Планировка", "commercial_building_type": "Вид здания",
    "state": "Доступность", "group_id": "Группа дублей",
    "group_spread_m": "Разброс координат",
}
ENUM_LABELS = {
    "included": "Включён / не начисляется", "excluded": "Сверх базовой ставки",
    "retail": "Торговое", "office": "Офис", "food": "Общепит",
    "warehouse": "Склад", "service": "Услуги", "other": "Другое",
}
MISSING = (None, "", "unknown")


def valid_item(item):
    return (
        item.get("source") in SOURCES
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item.get("source_id") or "")
        and item.get("label")
    )


def normalize_text(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def publication_label(publication):
    return f"{SOURCES.get(publication.source, publication.source)} · №{publication.pk}"


def snapshot_for(item, previous):
    if item is None:
        return {**previous, "state": "unavailable"}
    values = {key: item.get(key) for key in SNAPSHOT_FIELDS if key != "state"}
    return {**values, "state": "active"}


def save_version(publication, item, checked_at):
    previous = publication.snapshot
    snapshot = snapshot_for(item, previous)
    changed = previous != snapshot
    if changed:
        publication.version += 1
        publication.snapshot = snapshot
        changes = [
            label for key, label in SNAPSHOT_FIELDS.items()
            if previous.get(key) != snapshot.get(key)
        ]
        ListingPublicationVersion.objects.create(
            publication=publication, number=publication.version,
            captured_at=checked_at, snapshot=snapshot, changes=changes,
        )
    publication.checked_at = checked_at
    publication.next_check_at = checked_at + timedelta(hours=settings.METRAPI_REFRESH_HOURS)
    if item is not None:
        publication.group_id = item.get("group_id") or ""
    publication.save(update_fields=["version", "snapshot", "checked_at", "next_check_at", "group_id"])
    return changed


def comparable(value):
    return normalize_text(value) if isinstance(value, str) else value


def available_values(publications, extract):
    return [
        (publication_label(pub), value)
        for pub in publications
        if (value := extract(pub.snapshot)) not in MISSING
    ]


def display_value(value):
    return ENUM_LABELS.get(value, value) if isinstance(value, str) else value


def monthly_value(snapshot):
    rent, rate, area = (snapshot.get(key) for key in ("rent", "rate", "area"))
    if rent not in MISSING:
        return str(Decimal(rent).quantize(Decimal(".01")))
    if rate not in MISSING and area not in MISSING:
        return str((Decimal(rate) * Decimal(area)).quantize(Decimal(".01")))
    return None


def fact_value(snapshot, code):
    fact = (snapshot.get("facts") or {}).get(code, {})
    for field in ("value", "text", "boolean"):
        if field in fact:
            return fact[field]
    return None


def listing_conflicts(publications):
    conflicts = []

    def compare(key, label, values):
        if len({comparable(value) for _, value in values}) > 1:
            conflicts.append({
                "key": key, "label": label,
                "values": [
                    {"source": source, "value": display_value(value)}
                    for source, value in values
                ],
            })

    for key in ("label", *CARD_FIELDS, "lease_type", "heating", "parking", "commercial_layout", "commercial_building_type"):
        if key != "rent":
            compare(key, SNAPSHOT_FIELDS[key], available_values(publications, lambda s: s.get(key)))
    compare("rent", CARD_FIELDS["rent"], available_values(publications, monthly_value))
    codes = {
        code for pub in publications for code in (pub.snapshot.get("facts") or {})
    } - {"area_sqm", "floor"}
    for c in Characteristic.objects.filter(agency__isnull=True, code__in=codes).order_by("id"):
        compare(
            c.code, f"{c.label}, {c.unit}" if c.unit else c.label,
            available_values(publications, lambda s: fact_value(s, c.code)),
        )
    return conflicts


def project_card(premises, publications, conflicts, projection):
    updates = {}
    conflicted = {row["key"] for row in conflicts}
    known = {
        field: available_values(publications, lambda snapshot: snapshot.get(field))
        for field in CARD_FIELDS
    }

    def writable(field):
        return (
            (field not in FINANCE_FIELDS or premises.commercial_status != "confirmed")
            and (field not in {"city", "district"} or not premises.location_confirmed)
        )

    for field, previous in list(projection.items()):
        if not writable(field) or field in conflicted:
            continue
        model_field = premises._meta.get_field(field)
        if getattr(premises, field) != model_field.to_python(previous):
            projection.pop(field)
        elif publications and not known[field]:
            updates[field] = model_field.get_default()
            projection.pop(field)

    for field in CARD_FIELDS:
        if not writable(field):
            continue
        current = updates.get(field, getattr(premises, field))
        convert = premises._meta.get_field(field).to_python
        if current not in MISSING and field not in projection:
            continue
        if field in conflicted or field == "rate" and ({"rent", "area"} & conflicted):
            continue
        values = known[field]
        if not values:
            continue
        # Keep a single price basis. A comparable second price is visible in the source table.
        other = {"rent": "rate", "rate": "rent"}.get(field)
        if other and updates.get(other, getattr(premises, other)) is not None:
            continue
        value = values[0][1]
        projection[field] = value
        if current != convert(value):
            updates[field] = convert(value)
    if premises.listing_conflicts != conflicts:
        updates["listing_conflicts"] = conflicts
    return updates


def write_observations(premises, publication, item, user, *, only_height=False):
    source = source_name({"source_label": SOURCES[publication.source], "source_id": publication.source_id})
    facts = item.get("facts", {}) if item else {}
    records = premises.observations.filter(
        source_type="listing", source=source, status="unverified",
        confirmation="", superseded=False, scope="premises",
    )
    codes = set(facts) | set(records.values_list("characteristic__code", flat=True))
    if only_height:
        codes &= {"ceiling_height_m"}
    changed = False
    for c in Characteristic.objects.filter(agency__isnull=True, code__in=codes):
        fact = facts.get(c.code)
        prior = list(records.filter(characteristic=c))
        replaced = False
        if fact and item and user and not premises.observations.filter(
            characteristic=c, scope="premises", superseded=False, status="confirmed",
        ).exclude(source_type="listing").exists():
            value = fact.get("value")
            expected = (Decimal(value) if value is not None else None, fact.get("boolean"), fact.get("text", ""), fact.get("locator", ""))
            if len(prior) == 1 and (prior[0].value, prior[0].boolean, prior[0].text, prior[0].locator) == expected:
                continue
            Observation.objects.create(
                premises=premises, characteristic=c, name=c.label, unit=c.unit,
                source_type="listing", source=source, source_url=item["url"],
                observed_on=item["date"], status="unverified", author=user, **fact,
            )
            replaced = True
            changed = True
        # A refresh can retire only the unverified observations generated by this publication.
        if prior and (fact is None or replaced):
            premises.observations.filter(pk__in=[o.pk for o in prior]).update(superseded=True)
            changed = True
    return changed


def rebuild_card(premises, *, observations_changed=False, new=False, project=True):
    publications = list(premises.publications.order_by("pk"))
    active = [p for p in publications if p.snapshot.get("state") == "active"]
    media = getattr(premises, "listing", None)
    projection = dict(media.projected) if media else {}
    conflicts = listing_conflicts(active)
    updates = project_card(premises, active, conflicts, projection) if project else {}
    if updates or observations_changed:
        for field, value in updates.items():
            setattr(premises, field, value)
        if not new:
            premises.version += 1
        premises.save(update_fields=[*updates, "version", "updated_at"])
    if not active:
        return

    primary = active[0]
    photos = list(dict.fromkeys(url for pub in active for url in pub.snapshot.get("photos") or []))[:160]
    description = next((pub.snapshot["description"] for pub in active if pub.snapshot.get("description")), "")
    data = {
        "source": primary.source, "source_id": primary.source_id,
        "url": primary.snapshot.get("url") or premises.listing_url,
        "description": description, "photos": photos, "projected": projection,
    }
    if not media or any(getattr(media, key) != value for key, value in data.items()):
        PremisesListing.objects.update_or_create(premises=premises, defaults=data)


def existing_premises(agency_id, item, catalog):
    key = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"leasefit/metrapi/{agency_id}/{item['source']}/{item['source_id']}",
    )
    known = catalog.filter(creation_key=key).first()
    if not known and item.get("url"):
        known = catalog.filter(listing_url=item["url"]).order_by("pk").first()
    if known:
        return known, False

    group = item.get("group_id")
    spread = item.get("group_spread_m")
    if group and (spread is None or Decimal(spread) <= 100):
        matches = ListingPublication.objects.filter(agency_id=agency_id, group_id=group)
        own = set(matches.filter(source=item["source"]).values_list("premises_id", flat=True))
        city = normalize_text(item.get("city") or "")
        matching_ids = {
            pub.premises_id for pub in matches.exclude(source=item["source"])
            if pub.premises_id not in own
            and city and normalize_text(pub.snapshot.get("city") or "") == city
            and (
                pub.snapshot.get("group_spread_m") is None
                or Decimal(pub.snapshot["group_spread_m"]) <= 100
            )
        }
        if len(matching_ids) == 1:
            return catalog.get(pk=matching_ids.pop()), False
    return catalog.create(
        agency_id=agency_id, creation_key=key, label=item["label"],
        listing_url=item["url"], commercial_source=source_name(item),
        commercial_source_type="listing", commercial_date=item["date"],
    ), True


@transaction.atomic
def import_listings(agency_id, user, items, *, target_id=None):
    Agency.objects.select_for_update().get(pk=agency_id)
    catalog = Premises.objects.filter(agency_id=agency_id).select_for_update()
    checked_at = timezone.now()
    results = []
    for item in items:
        if not valid_item(item):
            continue
        target = None
        if target_id is not None:
            target = catalog.filter(pk=target_id).first()
            if not target or premises_listing_reference(target) != (item["source"], item["source_id"]):
                continue
        queries = ListingPublication.objects.filter(
            agency_id=agency_id, source=item["source"], source_id=item["source_id"]
        )
        publication = queries.first()
        new = False
        if publication is None:
            premises, new = (target, False) if target else existing_premises(agency_id, item, catalog)
            publication = queries.create(
                agency_id=agency_id, source=item["source"],
                source_id=item["source_id"], premises=premises,
            )
        else:
            if target and target.pk != publication.premises_id:
                continue
            premises = catalog.get(pk=publication.premises_id)
        previous = publication.version
        if save_version(publication, item, checked_at):
            enrolling_manual_card = target is not None and not previous
            technical = write_observations(
                premises, publication, item, user, only_height=enrolling_manual_card
            )
            rebuild_card(
                premises, observations_changed=technical, new=new, project=not enrolling_manual_card
            )
        results.append({
            "publication": publication, "premises": premises, "created": new,
            "linked": not new and not previous,
        })
    if any(row["created"] or row["linked"] for row in results):
        from .services import audit

        for row in results:
            if row["created"] or row["linked"]:
                audit(user, "premises.metrapi_imported", row["premises"].pk)
    return results


@transaction.atomic
def refresh_publication(publication_id, expected_checked_at, item, user=None):
    original = ListingPublication.objects.filter(pk=publication_id).first()
    if original is None:
        return False
    Agency.objects.select_for_update().get(pk=original.agency_id)
    publication = ListingPublication.objects.filter(pk=publication_id).first()
    if publication is None or publication.checked_at != expected_checked_at:
        return False
    premises = Premises.objects.select_for_update().get(pk=publication.premises_id)
    if not save_version(publication, item, timezone.now()):
        return False
    technical = write_observations(premises, publication, item, user)
    rebuild_card(premises, observations_changed=technical)
    return True
