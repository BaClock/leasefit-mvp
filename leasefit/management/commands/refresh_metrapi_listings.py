from datetime import timedelta
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from leasefit.listing_catalog import refresh_publication
from leasefit.metrapi import ListingUnavailable, MetrapiError, find_rental
from leasefit.models import ListingPublication, Membership


@transaction.atomic
def claim_due_publication():
    now = timezone.now()
    publication = (
        ListingPublication.objects.select_for_update(skip_locked=True)
        .filter(next_check_at__lte=now).order_by("next_check_at", "pk").first()
    )
    if publication:
        # Release the row before contacting Metrapi. A failed process is retried later.
        publication.next_check_at = now + timedelta(minutes=30)
        publication.save(update_fields=["next_check_at"])
    return publication


class Command(BaseCommand):
    help = "Recheck stored Metrapi publications when their refresh interval elapses."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check one due batch and exit.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum checks per batch.")

    def handle(self, *args, **options):
        if not settings.METRAPI_API_KEY:
            self.stdout.write("METRAPI_API_KEY is missing. Listing checks are disabled.")
            while not options["once"]:
                sleep(3600)
            return
        limit = max(1, min(options["limit"], 1000))
        while True:
            processed, changed = self.run_batch(limit)
            if processed or options["once"]:
                self.stdout.write(f"Checked {processed} publications, new versions: {changed}.")
            if options["once"]:
                return
            sleep(60)

    def run_batch(self, limit):
        processed = changed = 0
        for index in range(limit):
            publication = claim_due_publication()
            if publication is None:
                break
            if index:
                sleep(2.1)
            processed += 1
            try:
                item = find_rental((publication.source, publication.source_id))
            except ListingUnavailable:
                item = None
            except MetrapiError as exc:
                self.stderr.write(f"Publication {publication.pk}: {exc}")
                continue
            member = Membership.objects.filter(
                agency_id=publication.agency_id, active=True, role="manager",
                user__is_active=True,
            ).select_related("user").order_by("pk").first()
            if refresh_publication(
                publication.pk, publication.checked_at, item,
                member.user if member else None,
            ):
                changed += 1
        return processed, changed
