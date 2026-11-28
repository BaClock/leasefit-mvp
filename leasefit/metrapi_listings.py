"""Store the public listing separately from the agency's own premises data."""

from .metrapi import CEILING_HEIGHT_CODE, SOURCES, MetrapiError, listing_reference
from .models import Characteristic, Observation, PremisesListing


def source_name(item):
    return f"Metrapi / {item['source_label']} / {item['source_id']}"


def premises_listing_reference(premises):
    listing = getattr(premises, "listing", None)
    if listing:
        return listing.source, listing.source_id
    prefix, _, details = premises.commercial_source.partition(" / ")
    provider_name, _, source_id = details.partition(" / ")
    if prefix == "Metrapi" and source_id:
        source = next(
            (code for code, name in SOURCES.items() if name == provider_name), None
        )
        if source:
            return source, source_id
    try:
        return listing_reference(premises.listing_url)
    except MetrapiError:
        return None


def save_listing(premises, item, user, *, fill_height=False):
    """Apply a selected listing while holding the premises row lock."""
    listing = getattr(premises, "listing", None)
    previous = (listing.source, listing.source_id) if listing else None
    current = (item["source"], item["source_id"])
    if previous and previous != current:
        return False
    data = {
        "source": current[0],
        "source_id": current[1],
        "url": item["url"] or (listing.url if listing else premises.listing_url),
        "description": item.get("description") or (listing.description if listing else ""),
        "photos": item.get("photos") or (listing.photos if listing else []),
    }
    changed = not listing or any(
        getattr(listing, field) != value for field, value in data.items()
    )
    if changed:
        PremisesListing.objects.update_or_create(premises=premises, defaults=data)

    height = item["facts"].get(CEILING_HEIGHT_CODE)
    if fill_height and height:
        characteristic = Characteristic.objects.filter(
            agency__isnull=True, code=CEILING_HEIGHT_CODE
        ).first()
        if characteristic and not premises.observations.filter(
            characteristic=characteristic,
            scope="premises",
            superseded=False,
            value__isnull=False,
        ).exists():
            Observation.objects.create(
                premises=premises,
                characteristic=characteristic,
                name=characteristic.label,
                unit=characteristic.unit,
                source_type="listing",
                source=source_name(item),
                source_url=data["url"],
                observed_on=item["date"],
                status="unverified",
                author=user,
                **height,
            )
            premises.version += 1
            premises.save(update_fields=["version", "updated_at"])
            changed = True
    return changed
