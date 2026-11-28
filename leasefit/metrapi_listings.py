"""Store the public listing separately from the agency's own premises data."""

from .metrapi import SOURCES, MetrapiError, listing_reference


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
