"""Continue one bounded Metrapi discovery job at a time."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .listing_catalog import import_listings, valid_item
from .listing_filters import CATEGORIES, api_filters, matches
from .metrapi import CursorExpired, MetrapiError, MetrapiQuota, rental_page
from .models import Agency, ListingPublicationVersion, Membership, ParserConfiguration, ParserRun

SCHEDULE_ZONE = ZoneInfo("Asia/Novosibirsk")
PAGE_SIZE = 50
PAGES_PER_RUN = 12
DEFAULT_MAXIMUM = 200
MAXIMUM = PAGE_SIZE * PAGES_PER_RUN


def next_slot(now, interval, offset):
    local = now.astimezone(SCHEDULE_ZONE)
    anchor = datetime(2020, 1, 6, tzinfo=SCHEDULE_ZONE) + timedelta(minutes=offset)
    steps = (local - anchor) // timedelta(minutes=interval) + 1
    return anchor + timedelta(minutes=steps * interval)


def ensure_schedule(agency_id):
    now = timezone.now()
    known = set(ParserConfiguration.objects.filter(agency_id=agency_id).values_list("category", flat=True))
    missing = [
        ParserConfiguration(
            agency_id=agency_id, category=code, offset_minutes=index * 10,
            next_run_at=next_slot(now, 60, index * 10), priority=index + 1,
        )
        for index, (code, _) in enumerate(CATEGORIES) if code not in known
    ]
    if missing:
        ParserConfiguration.objects.bulk_create(missing, ignore_conflicts=True)


@transaction.atomic
def start_run():
    now = timezone.now()
    config = (
        ParserConfiguration.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(enabled=True, agency__parser_paused=False, next_run_at__lte=now)
        .order_by("next_run_at", "priority", "pk").first()
    )
    if config is None:
        return None
    config.runs.filter(status="running").update(
        status="partial", finished_at=now, error="Сервис перезапустился. Загрузка продолжится с сохранённой страницы.",
    )
    run = config.runs.create(scheduled_at=config.next_run_at)
    config.last_run_at = now
    config.next_run_at = next_slot(now, config.interval_minutes, config.offset_minutes)
    config.save(update_fields=["last_run_at", "next_run_at"])
    return config, run


def discover(config, run, user):
    filters = config.filters
    categories = [config.category]
    params = api_filters(filters, categories)
    state = dict(config.cursor or {"mode": "items", "since": timezone.now().isoformat()})
    maximum = filters.get("maximum") or DEFAULT_MAXIMUM
    stats = dict.fromkeys(("received", "saved", "created", "updated", "duplicates", "unavailable", "errors"), 0)
    previous = config.runs.filter(pk__lt=run.pk).order_by("-pk").first()
    if previous:
        stats["unavailable"] = sum(
            config.category in (version.snapshot.get("categories") or ())
            for version in ListingPublicationVersion.objects.filter(
                publication__agency_id=config.agency_id,
                captured_at__gte=previous.started_at, captured_at__lt=run.started_at,
                snapshot__state="unavailable",
            ).only("snapshot")
            if "Доступность" in version.changes
        )
    status, error = "partial", ""
    try:
        for _ in range(PAGES_PER_RUN):
            remaining = maximum - stats["saved"]
            if remaining <= 0:
                break
            limit = min(PAGE_SIZE, remaining)
            feed = state["mode"] == "feed"
            rows, cursor, received = rental_page(
                params, cursor=state.get("position"), since=state.get("since") if feed else None,
                feed=feed, limit=limit,
            )
            items = [row for row in rows if valid_item(row) and matches(row, filters, categories)]
            with transaction.atomic():
                # Commit the data and its cursor together. Configuration changes win a race.
                Agency.objects.select_for_update().get(pk=config.agency_id)
                current = ParserConfiguration.objects.select_for_update().get(pk=config.pk)
                if not current.enabled or current.filters != config.filters:
                    break
                imported = import_listings(config.agency_id, user, items)
                stats["received"] += received
                stats["saved"] += len(imported)
                stats["created"] += sum(row["created"] for row in imported)
                stats["updated"] += sum(row["changed"] and not row["created"] and not row["linked"] for row in imported)
                stats["duplicates"] += sum(not row["created"] for row in imported)
                if not feed and cursor is None:
                    state = {"mode": "feed", "since": state["since"]}
                else:
                    state["position"] = cursor
                    if feed:
                        state.pop("since", None)
                current.cursor = state
                current.save(update_fields=["cursor"])
                run.stats = stats
                run.save(update_fields=["stats"])
            # An empty filtered feed page may still advance the cursor over other categories.
            if feed and received == 0:
                status = "complete"
                break
    except CursorExpired as exc:
        ParserConfiguration.objects.filter(pk=config.pk, filters=filters).update(cursor={})
        error = str(exc)
    except MetrapiError as exc:
        status = "quota" if isinstance(exc, MetrapiQuota) else "error"
        stats["errors"] += 1
        error = str(exc)
    finished = timezone.now()
    ParserRun.objects.filter(pk=run.pk).update(status=status, stats=stats, error=error, finished_at=finished)
    if status == "complete":
        ParserConfiguration.objects.filter(pk=config.pk, filters=filters).update(last_success_at=finished)


def run_due_discovery():
    claimed = start_run()
    if claimed is None:
        return False
    config, run = claimed
    owner = Membership.objects.filter(
        agency_id=config.agency_id, active=True, role="manager", user__is_active=True,
    ).select_related("user").order_by("pk").first()
    if owner is None:
        ParserRun.objects.filter(pk=run.pk).update(
            status="error", error="В агентстве нет активного руководителя.",
            finished_at=timezone.now(), stats={"errors": 1},
        )
    else:
        discover(config, run, owner.user)
    return True
