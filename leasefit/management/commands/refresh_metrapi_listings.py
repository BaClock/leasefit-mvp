from datetime import timedelta
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import DateTimeField, ExpressionWrapper, F, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from leasefit.listing_catalog import refresh_publication
from leasefit.metrapi import ListingUnavailable, MetrapiError, MetrapiQuota, find_rental
from leasefit.metrapi_limits import locked_file, quota_paused
from leasefit.models import ListingPublication, Membership, ParserConfiguration
from leasefit.parser_schedule import ensure_schedule, run_due_discovery


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


def shorten_existing_schedule():
    now = timezone.now()
    interval = timedelta(hours=settings.METRAPI_REFRESH_HOURS)
    next_check = ExpressionWrapper(F("checked_at") + interval, output_field=DateTimeField())
    return ListingPublication.objects.filter(next_check_at__gt=now + interval).update(
        next_check_at=Greatest(Value(now), Coalesce(next_check, Value(now))),
    )


class Command(BaseCommand):
    help = "Discover new listings by category and recheck stored Metrapi publications."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check one due batch and exit.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum checks per batch.")
        parser.add_argument("--discover-only", action="store_true", help="Process the scheduled catalog without rechecking publications.")

    def handle(self, *args, **options):
        if not settings.METRAPI_API_KEY:
            self.stdout.write("METRAPI_API_KEY is missing. Listing checks are disabled.")
            while not options["once"]:
                sleep(3600)
            return
        shorten_existing_schedule()
        for agency_id in Membership.objects.filter(
            active=True, role="manager", user__is_active=True,
        ).order_by().values_list("agency_id", flat=True).distinct():
            ensure_schedule(agency_id)
        limit = max(1, min(options["limit"], 1000))
        while True:
            with locked_file("worker", blocking=False) as lock:
                discovered = lock is not False and run_due_discovery()
            processed, changed = (0, 0) if options["discover_only"] else self.run_batch(
                limit, yield_to_discovery=not options["once"],
            )
            if processed or options["once"]:
                self.stdout.write(f"Checked {processed} publications, new versions: {changed}.")
            if options["once"]:
                return
            if not discovered and not processed:
                sleep(15)

    def run_batch(self, limit, *, yield_to_discovery=False):
        processed = changed = 0
        if quota_paused():
            return processed, changed
        for index in range(limit):
            if index and yield_to_discovery and ParserConfiguration.objects.filter(
                enabled=True, agency__parser_paused=False, next_run_at__lte=timezone.now(),
            ).exists():
                break
            publication = claim_due_publication()
            if publication is None:
                break
            if index and not settings.METRAPI_STATE_DIRECTORY:
                sleep(2.1)
            processed += 1
            try:
                item = find_rental((publication.source, publication.source_id))
            except ListingUnavailable:
                item = None
            except MetrapiQuota as exc:
                self.stderr.write(str(exc))
                break
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
