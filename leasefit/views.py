import hashlib, json, uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path
from django import forms
from django.conf import settings
from django.core import signing
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.fields.json import KeyTextTransform
from django.http import Http404, HttpResponse, FileResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET, require_http_methods
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from .business import FORMATS, TEMPLATE_VERSION, recommendations
from .business_forms import BusinessSetupForm
from .outcomes import GROUPS as OUTCOME_GROUPS, assess_outcome
from .geography import district_key, requested_districts
from .metric_forms import FinancialPlanForm
from .metrics import METHODS, INSUFFICIENT
from .regulations import (
    REFERENCES,
    REGISTRY_VERSION,
    STATUS as REGULATORY_STATUS,
    regulatory_checklist,
)
from .operations import GENERAL_ACCESS, LEGACY_BY_REPLACEMENT, STRUCTURED, value_label
from .recommendations import (
    for_request_city,
    recommend_catalog_premises,
    technical_filters,
)
from .models import *
from .forms import *
from .services import *
from .characteristics import GROUPS, STANDARD, resolve_characteristic
from .metrapi import (
    CEILING_HEIGHT_CODE, ListingUnavailable, MetrapiError, find_rental,
    listing_reference, search_rentals,
)
from .metrapi_listings import premises_listing_reference
from .listing_catalog import (
    SNAPSHOT_FIELDS, display_value, import_listings, publication_label, refresh_publication,
)


def workspace(fn):
    @login_required
    @wraps(fn)
    def inner(request, *args, **kwargs):
        m = getattr(request.user, "membership", None)
        if not m or not m.active or not request.user.is_active:
            raise PermissionDenied("Доступ закрыт. Обратитесь к руководителю.")
        request.member = m
        return fn(request, *args, **kwargs)

    return inner


def accessible(request):
    q = ClientRequest.objects.filter(agency=request.member.agency)
    return q if request.member.role == "manager" else q.filter(owner=request.user)


def load_request(request, pk, write=False, lock=False, working=True):
    q = accessible(request)
    if lock:
        q = q.select_for_update()
    obj = get_object_or_404(q, pk=pk)
    if write:
        if obj.owner_id != request.user.pk:
            raise PermissionDenied("Запрос коллеги доступен только для чтения.")
        if working and (obj.archived or obj.stage == "done"):
            raise PermissionDenied("Сначала явно верните запрос в работу.")
    return obj


def check_version(request, obj):
    try:
        v = int(request.POST.get("version", ""))
    except (TypeError, ValueError):
        return False
    return v == obj.version


def conflict(request, template, context, form=None):
    msg = "Запись изменена в другой вкладке. Ваш ввод сохранён на экране. Откройте актуальную версию в новой вкладке и сопоставьте изменения."
    if form:
        form.add_error(None, msg)
    else:
        messages.error(request, msg)
    return render(request, template, context, status=409)


def tabs_context(request, obj, section):
    return {
        "req": obj,
        "section": section,
        "can_edit": obj.owner_id == request.user.pk
        and not obj.archived
        and obj.stage != "done",
        "is_owner": obj.owner_id == request.user.pk,
    }


@workspace
@require_GET
def requests_list(request):
    qs = (
        accessible(request)
        .annotate(object_count=Count("premises"))
        .order_by("-updated_at")
    )
    stage = request.GET.get("stage", "active_all")
    search = request.GET.get("q", "").strip()
    qs = qs.filter(archived=stage == "archive")
    if stage in ["draft", "active", "done"]:
        qs = qs.filter(stage=stage)
    if search:
        qs = qs.filter(title__icontains=search)
    owner = request.GET.get("owner", "")
    if owner.isdigit() and request.member.role == "manager":
        qs = qs.filter(owner_id=owner)
    members = Membership.objects.filter(agency=request.member.agency).select_related(
        "user"
    )
    return render(
        request,
        "requests.html",
        {
            "requests": qs,
            "stage": stage,
            "search": search,
            "members": members,
            "owner_filter": owner,
        },
    )


@workspace
@transaction.atomic
def profile(request, pk=None):
    if not pk and request.method == "POST":
        User.objects.select_for_update().get(pk=request.user.pk)
    obj = (
        load_request(
            request, pk, write=request.method == "POST", lock=request.method == "POST"
        )
        if pk
        else ClientRequest(owner=request.user, agency=request.member.agency)
    )
    original_districts = obj.districts
    form = RequestForm(request.POST or None, instance=obj)
    grouped = GroupedRequirementsForm(request.POST or None, request_obj=obj)
    custom_qs = (
        obj.requirements.filter(
            Q(characteristic__agency__isnull=False)
            | Q(characteristic__code__in=grouped.ambiguous_codes)
        )
        if obj.pk
        else Requirement.objects.none()
    )
    formset = CustomRequirementsFormSet(
        request.POST or None,
        instance=obj,
        prefix="custom",
        queryset=custom_qs,
        form_kwargs={"agency": request.member.agency},
    )
    if request.method == "POST":
        # Duplicate create is safe even if the original form has since changed.
        if not pk:
            try:
                key = uuid.UUID(request.POST.get("creation_key", ""))
            except (ValueError, TypeError, AttributeError):
                key = None
            existing = (
                accessible(request).filter(creation_key=key, owner=request.user).first()
                if key
                else None
            )
            if existing:
                return redirect(
                    (
                        "business_setup"
                        if request.POST.get("next") == "business"
                        else "refine"
                    ),
                    pk=existing.pk,
                )
        valid = form.is_valid() & grouped.is_valid() & formset.is_valid()
        ctx = {
            **tabs_context(request, obj, "profile"),
            "form": form,
            "formset": formset,
            "grouped": grouped,
            "new": not pk,
        }
        if pk and not check_version(request, obj):
            return conflict(request, "profile.html", ctx, form)
        if valid:
            obj = form.save(commit=False)
            if not pk:
                obj.creation_key = form.cleaned_data["creation_key"]
            if pk and (
                original_districts != obj.districts
                or any(name != "districts" for name in form.changed_data)
                or grouped.has_changed()
                or formset.has_changed()
            ):
                obj.version += 1
            obj.save()
            grouped.save(obj)
            formset.instance = obj
            formset.save()
            audit(request.user, "profile.updated" if pk else "request.created", obj.pk)
            messages.success(
                request,
                (
                    "Профиль сохранён. Предыдущие оценки требуют пересчёта."
                    if pk
                    else "Запрос создан. Добавьте варианты помещений."
                ),
            )
            return redirect(
                (
                    "business_setup"
                    if request.POST.get("next") == "business"
                    else "refine"
                ),
                pk=obj.pk,
            )
    return render(
        request,
        "profile.html",
        {
            **tabs_context(request, obj, "profile"),
            "form": form,
            "formset": formset,
            "grouped": grouped,
            "new": not pk,
            "regulatory": regulatory_checklist(obj),
            "regulatory_references": REFERENCES,
        },
    )


@workspace
@require_POST
@transaction.atomic
def lifecycle(request, pk):
    obj = load_request(request, pk, write=True, lock=True, working=False)
    if not check_version(request, obj):
        messages.error(
            request,
            "Состояние уже изменилось. Повторите действие с актуальной страницы.",
        )
        return redirect("profile", pk=pk)
    action = request.POST.get("action")
    if action == "archive":
        obj.archived = True
    elif action == "restore":
        obj.archived = False
    elif action in ["active", "done"] and not obj.archived:
        obj.stage = action
    else:
        raise Http404
    obj.version += 1
    obj.save()
    audit(request.user, "request." + action, pk)
    return redirect("profile", pk=pk)


@workspace
@require_http_methods(["GET", "POST"])
@transaction.atomic
def delete_request(request, pk):
    deleting = request.method == "POST"
    obj = load_request(request, pk, write=True, lock=deleting, working=False)
    if deleting and (
        check_version(request, obj)
        and request.POST.get("selection_version") == str(obj.selection_version)
    ):
        legacy_files = obj.legacy_attachments.all()
        if legacy_files.exists():
            affected = Premises.objects.filter(
                pk__in=legacy_files.values("premises_id")
            )
            list(
                affected.select_for_update().order_by("pk").values_list("pk", flat=True)
            )
            # These documents are private to the request even in the shared catalog.
            Observation.objects.filter(document__in=legacy_files).update(document=None)
            affected.update(version=F("version") + 1)
            legacy_files.delete()
        obj.evaluations.all().delete()
        obj.reports.all().delete()
        obj.delete()
        audit(request.user, "request.deleted", pk)
        request.session.pop(f"report-comment-{pk}", None)
        messages.success(request, "Запрос удалён.")
        return redirect("requests")
    return render(
        request,
        "confirm_delete.html",
        {
            "section": "profile",
            "title": "Удалить запрос",
            "label": obj.title,
            "description": (
                "Удалятся требования, проверки, PDF и вложения этого запроса. "
                "Общие карточки помещений останутся в каталоге."
            ),
            "version": obj.version,
            "selection_version": obj.selection_version,
            "cancel_url": reverse("profile", args=[pk]),
        },
        status=409 if deleting else 200,
    )


@workspace
@require_GET
def objects(request, pk):
    obj = load_request(request, pk)
    requirements = active_requirements(obj)
    cards, ranking_ready = rank_premises(obj, requirements=requirements)
    catalog = catalog_scope(request)
    selected_cards = {card["p"].pk: card for card in cards}
    catalog_count = catalog.count()
    city_catalog = for_request_city(catalog, obj)
    districts = list(
        city_catalog.exclude(district="")
        .order_by("district")
        .values_list("district", flat=True)
        .distinct()
    )
    district_names, unmatched_districts = requested_districts(obj, districts)
    district_keys = {district_key(name) for name in district_names or ()}
    form = RecommendationFilterForm(
        request.GET,
        districts=[
            name
            for name in districts
            if district_names is None or district_key(name) in district_keys
        ],
    )
    recommendations, counts = [], {}
    if catalog_count and ranking_ready and form.is_valid():
        recommendations, counts = recommend_catalog_premises(
            obj,
            catalog,
            requirements,
            form.cleaned_data,
            selected_cards,
            district_names=district_names,
        )
    page = Paginator(recommendations, 20).get_page(request.GET.get("page"))
    groups = {
        group: [card for card in page if card["catalog_group"] == group]
        for group in OUTCOME_GROUPS
    }
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    for card in cards:
        card["compare_url"] = comparison_url(obj, [card["p"]])
    return render(
        request,
        "objects.html",
        {
            **tabs_context(request, obj, "objects"),
            "cards": cards,
            "ranking_ready": ranking_ready,
            "compare_url": comparison_url(obj, [card["p"] for card in cards[:4]]),
            "recommendation_groups": groups,
            "recommendation_counts": counts,
            "recommendation_page": page,
            "recommendation_filters": form,
            "catalog_count": catalog_count,
            "profile_districts": district_names,
            "unmatched_districts": unmatched_districts,
            "city_excluded": (
                catalog_count - city_catalog.count()
                if obj.city and catalog_count
                else 0
            ),
            "filter_query": filter_params.urlencode(),
            "action_query": request.GET.urlencode(),
            "normative_questions": [
                row for row in regulatory_checklist(obj) if row["status"] == "pending"
            ],
        },
    )


def catalog_scope(request):
    return Premises.objects.filter(agency=request.member.agency)


@workspace
@require_http_methods(["GET", "POST"])
@transaction.atomic
def delete_premises(request, object_id):
    p = get_object_or_404(catalog_scope(request), pk=object_id)
    deleting = request.method == "POST"
    if deleting:
        # Request edits, evaluations and report issuance lock requests before premises.
        linked_requests = p.requests.order_by("pk").values_list("pk", flat=True)
        locked_ids = set(
            ClientRequest.objects.filter(pk__in=linked_requests)
            .select_for_update()
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        p = get_object_or_404(catalog_scope(request).select_for_update(), pk=object_id)
    links = list(p.request_links.order_by("pk").values_list("pk", "request_id"))
    request_ids = {request_id for _, request_id in links}
    link_version = hashlib.sha256(repr(links).encode()).hexdigest()
    blocked = request.member.role != "manager" and (
        p.requests.exclude(owner=request.user).exists()
        or p.evaluations.exclude(request__owner=request.user).exists()
        or p.attachments.filter(origin_request__isnull=False)
        .exclude(origin_request__owner=request.user)
        .exists()
    )
    if (
        deleting
        and not blocked
        and (
            request_ids <= locked_ids
            and check_version(request, p)
            and request.POST.get("selection_version") == link_version
        )
    ):
        p.request_links.all().delete()
        # Observations can protect the source documents stored on this premises.
        p.observations.all().delete()
        p.delete()
        ClientRequest.objects.filter(pk__in=request_ids).update(
            selection_version=F("selection_version") + 1
        )
        audit(request.user, "premises.deleted", object_id)
        messages.success(request, "Помещение удалено из каталога и связанных запросов.")
        return redirect("all_premises")
    return render(
        request,
        "confirm_delete.html",
        {
            "section": "all_premises",
            "title": "Удалить помещение",
            "label": p.label,
            "description": (
                f"Карточка исчезнет из каталога и связанных запросов ({len(links)}). "
                "Характеристики, проверки и вложения удалятся. Выпущенные PDF сохранятся."
            ),
            "blocked": (
                "У помещения есть данные запросов коллег. Удалить его может руководитель."
                if blocked
                else ""
            ),
            "version": p.version,
            "selection_version": link_version,
            "cancel_url": reverse("catalog_premises", args=[object_id]),
        },
        status=403 if blocked else 409 if deleting else 200,
    )


def available_files(request, p):
    return p.attachments.filter(
        Q(origin_request__isnull=True) | Q(origin_request__in=accessible(request))
    ).order_by("id")


def property_load(request, pk, object_id, write=False, lock=False):
    req = load_request(request, pk, write=write, lock=lock) if pk is not None else None
    qs = catalog_scope(request)
    if req is not None:
        qs = qs.filter(requests=req)
    if lock:
        qs = qs.select_for_update()
    p = (
        get_object_or_404(qs, pk=object_id)
        if object_id is not None
        else Premises(agency=request.member.agency)
    )
    return req, p


def property_redirect(req, p):
    return (
        redirect("premises", pk=req.pk, object_id=p.pk)
        if req is not None
        else redirect("catalog_premises", object_id=p.pk)
    )


def property_page(
    request,
    req,
    p,
    form=None,
    obsform=None,
    uploadform=None,
    customform=None,
    context_form=None,
    status=200,
):
    if form is None:
        form = PremisesForm(instance=p)
    shared = p.pk and p.request_links.count() > 1
    if shared:
        form.fields["confirm_shared"] = forms.BooleanField(
            label="Я понимаю: общие изменения затронут все связанные запросы.",
            required=True,
        )
    files = available_files(request, p) if p.pk else Attachment.objects.none()
    if obsform is None:
        initial = {"observed_on": timezone.localdate()}
        code = request.GET.get("characteristic")
        choice = (
            Characteristic.objects.filter(
                Q(agency__isnull=True) | Q(agency=request.member.agency), code=code
            ).first()
            if code
            else None
        )
        if choice:
            initial["characteristic"] = choice.pk
        obsform = ObservationForm(
            agency=request.member.agency,
            premises=p,
            visible_files=files,
            initial=initial,
        )
    if shared:
        obsform.fields["confirm_shared"] = forms.BooleanField(
            label="Уточнение общей характеристики изменит оценки во всех связанных запросах.",
            required=True,
        )
    groups = []
    observations = (
        list(p.observations.select_related("characteristic").order_by("id"))
        if p.pk
        else []
    )
    for key, title in [*GROUPS, ("custom", "Дополнительные характеристики")]:
        entries = []
        for c in Characteristic.objects.filter(
            Q(agency__isnull=True) | Q(agency=request.member.agency), group=key
        ).order_by("id"):
            records = [o for o in observations if o.characteristic_id == c.pk]
            live = [o for o in records if not o.superseded]
            latest = live[-1] if live else None
            entries.append(
                {
                    "characteristic": c,
                    "primary": c.group == "custom"
                    or (
                        c.code == CEILING_HEIGHT_CODE
                        and latest is not None
                        and latest.value is not None
                    )
                    or any(d["code"] == c.code and d["primary"] for d in STANDARD),
                    "records": records,
                    "latest": latest,
                    "conflict": len(
                        {
                            (
                                o.value,
                                o.boolean,
                                o.text,
                                o.unit,
                                json.dumps(o.structured_value, sort_keys=True),
                            )
                            for o in live
                            if o.status != "unknown"
                        }
                    )
                    > 1,
                }
            )
        if entries:
            groups.append(
                {
                    "title": title,
                    "entries": entries,
                    "has_advanced": any(not e["primary"] for e in entries),
                }
            )
    ctx = {
        **(
            tabs_context(request, req, "objects")
            if req
            else {"section": "all_premises", "can_edit": True}
        ),
        "p": p,
        "form": form,
        "obsform": obsform,
        "uploadform": uploadform or AttachmentForm(),
        "customform": customform or CharacteristicForm(),
        "groups": groups,
        "files": files,
        "new_object": not p.pk,
        "shared": shared,
        "use_count": p.request_links.count() if p.pk else 0,
    }
    if p.pk:
        ctx["listing"] = getattr(p, "listing", None)
        ctx["publications"] = [
            {"entry": publication, "label": publication_label(publication)}
            for publication in p.publications.order_by("pk")
        ]
        ctx["can_refresh_listing"] = bool(
            ctx["can_edit"]
            and settings.METRAPI_API_KEY
            and (request.user.is_superuser or request.member.role == "manager")
            and premises_listing_reference(p)
        )
    ctx["visible_requests"] = (
        accessible(request).filter(premises=p).distinct()
        if p.pk
        else ClientRequest.objects.none()
    )
    if req and p.pk:
        link = get_object_or_404(RequestPremises, request=req, premises=p)
        ev = current_evaluation(p, req)
        costs = economics(p, req, context=link)
        ctx.update(
            context_form=context_form or ContextForm(instance=link),
            ev=ev,
            economics=costs,
            outcome=(
                ev.result["outcome"]
                if ev
                else assess_outcome(req, p, "clarify", [], costs, link)
            ),
            regulatory=regulatory_checklist(req, link, with_notes=True),
            regulatory_references=REFERENCES,
            regulatory_status=REGULATORY_STATUS,
            review_link=link,
        )
    else:
        ctx["economics"] = economics(p)
    return render(request, "premises.html", ctx, status=status)


@workspace
@transaction.atomic
def premises_edit(request, pk=None, object_id=None):
    req, p = property_load(
        request,
        pk,
        object_id,
        write=request.method == "POST",
        lock=request.method == "POST",
    )
    form = PremisesForm(request.POST or None, instance=p)
    if p.pk and p.request_links.count() > 1:
        form.fields["confirm_shared"] = forms.BooleanField(
            label="Изменения затронут все связанные запросы. Подтверждаю сохранение.",
            required=True,
        )
    if request.method == "POST":
        if not object_id:
            try:
                key = uuid.UUID(request.POST.get("creation_key", ""))
            except (ValueError, TypeError, AttributeError):
                key = None
            previous = (
                catalog_scope(request)
                .select_for_update()
                .filter(creation_key=key)
                .first()
                if key
                else None
            )
            if previous:
                if req:
                    RequestPremises.objects.get_or_create(
                        request=req, premises=previous
                    )
                return property_redirect(req, previous)
        valid = form.is_valid()
        if object_id and not check_version(request, p):
            form.add_error(
                None,
                "Карточка изменилась в другой вкладке. Сопоставьте актуальные сведения.",
            )
            return property_page(request, req, p, form=form, status=409)
        if valid:
            p = form.save(commit=False)
            p.agency = request.member.agency
            if object_id:
                p.version += 1
            else:
                p.creation_key = form.cleaned_data["creation_key"]
            p.save()
            if req:
                RequestPremises.objects.get_or_create(request=req, premises=p)
            if (
                catalog_scope(request)
                .filter(
                    label__iexact=p.label,
                    floor__iexact=p.floor,
                    number__iexact=p.number,
                )
                .exclude(pk=p.pk)
                .exists()
            ):
                messages.warning(
                    request,
                    "Возможно, такая карточка уже есть в каталоге. Записи не объединяются автоматически.",
                )
            audit(
                request.user,
                "premises.updated" if object_id else "premises.created",
                p.pk,
            )
            messages.success(
                request,
                "Общая карточка сохранена. Связанные оценки потребуют пересчёта.",
            )
            return property_redirect(req, p)
    return property_page(request, req, p, form=form)


@workspace
@require_POST
@transaction.atomic
def add_observation(request, object_id, pk=None):
    req, p = property_load(request, pk, object_id, write=True, lock=True)
    form = ObservationForm(
        request.POST,
        agency=request.member.agency,
        premises=p,
        visible_files=available_files(request, p),
    )
    if p.request_links.count() > 1:
        form.fields["confirm_shared"] = forms.BooleanField(
            label="Подтверждаю изменение общей характеристики.", required=True
        )
    if form.is_valid():
        if p.observations.filter(
            creation_key=form.cleaned_data["creation_key"]
        ).exists():
            return property_redirect(req, p)
        if not check_version(request, p):
            form.add_error(
                None, "Карточка изменилась. Обновите сведения перед добавлением."
            )
            return property_page(request, req, p, obsform=form, status=409)
        o = form.save(commit=False)
        o.premises = p
        o.author = request.user
        o.creation_key = form.cleaned_data["creation_key"]
        o.save()
        p.version += 1
        p.save()
        audit(request.user, "observation.added", o.pk)
        messages.success(
            request,
            "Сведения сохранены. При расхождении источников выберите актуальный с основанием.",
        )
        return property_redirect(req, p)
    return property_page(request, req, p, obsform=form, status=400)


@workspace
@require_POST
@transaction.atomic
def resolve(request, object_id, observation_id, pk=None):
    req, p = property_load(request, pk, object_id, write=True, lock=True)
    o = get_object_or_404(p.observations, pk=observation_id)
    if p.request_links.count() > 1 and not request.POST.get("confirm_shared"):
        messages.error(
            request, "Подтвердите изменение общей карточки для нескольких запросов."
        )
        return property_redirect(req, p)
    reason = request.POST.get("reason", "").strip()
    if not check_version(request, p):
        messages.error(request, "Карточка изменилась. Обновите страницу.")
        return property_redirect(req, p)
    if (
        not reason
        or len(reason) > 700
        or o.superseded
        or o.source_type in ["archive", "listing"]
        or o.scope != "premises"
        or (
            o.value is None
            and o.boolean is None
            and not o.text
            and o.structured_value is None
        )
    ):
        messages.error(
            request,
            "Нужны актуальное значение помещения, документ / ответ и основание выбора.",
        )
    else:
        p.observations.filter(characteristic_id=o.characteristic_id).exclude(
            pk=o.pk
        ).update(superseded=True)
        o.status = "confirmed"
        o.confirmation = reason
        o.save()
        p.version += 1
        p.save()
        audit(request.user, "conflict.resolved", o.pk)
        messages.success(
            request,
            "Источник выбран. История сохранена; пересчитайте связанные оценки.",
        )
    return property_redirect(req, p)


@workspace
@require_POST
@transaction.atomic
def upload(request, object_id, pk=None):
    req, p = property_load(request, pk, object_id, write=True, lock=True)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        if not check_version(request, p):
            form.add_error(
                None, "Карточка изменилась. Обновите страницу и выберите файл заново."
            )
            return property_page(request, req, p, uploadform=form, status=409)
        f = form.cleaned_data["file"]
        data = f.read()
        digest = hashlib.sha256(data).hexdigest()
        old = p.attachments.filter(sha256=digest).first()
        if (
            old
            and old.origin_request_id
            and not accessible(request).filter(pk=old.origin_request_id).exists()
        ):
            form.add_error(
                None, "Файл нельзя добавить в этот раздел. Выберите другой исходник."
            )
            return property_page(request, req, p, uploadform=form, status=400)
        f, created = Attachment.objects.get_or_create(
            premises=p,
            sha256=digest,
            defaults={
                "filename": Path(f.name).name[:250],
                "data": data,
                "content_type": f.verified_type,
                "author": request.user,
            },
        )
        if created:
            audit(request.user, "attachment.uploaded", f.pk)
        messages.success(
            request,
            "Технический документ сохранён для сотрудников агентства; в PDF исходник не включается.",
        )
        return property_redirect(req, p)
    return property_page(request, req, p, uploadform=form, status=400)


@workspace
@require_POST
@transaction.atomic
def custom_characteristic(request, object_id):
    _, p = property_load(request, None, object_id, write=True, lock=True)
    form = CharacteristicForm(request.POST)
    if form.is_valid():
        c = resolve_characteristic(
            request.member.agency_id,
            form.cleaned_data["name"],
            form.cleaned_data["unit"],
            form.cleaned_data["kind"],
        )
        return redirect(
            reverse("catalog_premises", args=[p.pk])
            + "?characteristic="
            + c.code
            + "#evidence-form"
        )
    return property_page(request, None, p, customform=form, status=400)


@workspace
@require_POST
@transaction.atomic
def request_context(request, pk, object_id):
    req = load_request(request, pk, write=True, lock=True)
    link = get_object_or_404(RequestPremises, request=req, premises_id=object_id)
    form = ContextForm(request.POST, instance=link)
    if form.is_valid():
        if not check_version(request, link):
            messages.error(request, "Условия запроса изменились. Обновите страницу.")
            return redirect("premises", pk=pk, object_id=object_id)
        if form.has_changed():
            link = form.save(commit=False)
            link.version += 1
            link.save()
            audit(request.user, "request.premises_context.updated", link.pk)
        return redirect("premises", pk=pk, object_id=object_id)
    return property_page(request, req, link.premises, context_form=form, status=400)


@workspace
@require_POST
@transaction.atomic
def regulatory_review(request, pk, object_id):
    req, p = property_load(request, pk, object_id, write=True, lock=True)
    link = get_object_or_404(
        RequestPremises.objects.select_for_update(), request=req, premises=p
    )
    code = request.POST.get("rule")
    rules = {rule["code"]: rule for rule in regulatory_checklist(req, link)}
    rule = rules.get(code)
    if not rule or not rule["reviewable"]:
        raise Http404
    status = request.POST.get("status")
    note = request.POST.get("note", "").strip()
    if (
        not check_version(request, link)
        or request.POST.get("profile_version") != str(req.version)
        or request.POST.get("premises_version") != str(p.version)
    ):
        messages.error(
            request, "Данные изменились. Проверьте текущие сведения перед отметкой."
        )
    elif (
        status not in REGULATORY_STATUS
        or len(note) > 700
        or (status != "pending" and not note)
    ):
        messages.error(
            request, "Укажите статус и основание проверки длиной до 700 символов."
        )
    else:
        link.regulatory_reviews = {
            **link.regulatory_reviews,
            code: {
                "status": status,
                "note": note,
                "registry": REGISTRY_VERSION,
                "profile": req.version,
                "premises": p.version,
                "actor": request.user.pk,
                "reviewed_on": timezone.localdate().isoformat(),
            },
        }
        link.version += 1
        link.save(update_fields=["regulatory_reviews", "version"])
        audit(request.user, "request.regulatory.reviewed", f"{link.pk}:{code}")
        messages.success(
            request,
            "Предварительная отметка сохранена. Пересчитайте оценку перед выпуском отчёта.",
        )
    return redirect(reverse("premises", args=[pk, object_id]) + "#normative-checklist")


@workspace
@require_GET
def attachment_download(request, file_id):
    f = get_object_or_404(Attachment.objects.select_related("premises"), pk=file_id)
    get_object_or_404(catalog_scope(request), pk=f.premises_id)
    if f.origin_request_id:
        load_request(request, f.origin_request_id)
    audit(request.user, "attachment.downloaded", f.pk)
    from io import BytesIO

    response = FileResponse(
        BytesIO(bytes(f.data)),
        as_attachment=True,
        filename=f.filename,
        content_type=f.content_type,
    )
    response["Cache-Control"] = "private, no-store"
    return response


@workspace
@transaction.atomic
def compare(request, pk):
    write = request.method == "POST"
    read_only_action = request.POST.get("action") in ("show", "preview")
    obj = load_request(
        request,
        pk,
        write=write and not read_only_action,
        lock=write and not read_only_action,
    )
    differences = (request.POST if write else request.GET).get("differences") == "1"
    data = request.POST if write else request.GET if "objects" in request.GET else None
    if data is None:
        data = {
            "objects": list(obj.premises.values_list("id", flat=True)[:4]),
            "creation_key": str(uuid.uuid4()),
        }
    elif not write:
        data = data.copy()
        data["creation_key"] = str(uuid.uuid4())
    if not write:
        data = data.copy()
        data["comment"] = request.session.get(f"report-comment-{obj.pk}", "")
        data.update(request.session.get(f"report-options-{obj.pk}", {}))
        data.setdefault("report_type", CLIENT_SHORT)
    form = SelectionForm(data, request_obj=obj, minimum=1)
    selected, rows, columns, preview = [], [], [], None
    preview_error = ""
    choices = report_parameter_choices(obj)
    action = request.POST.get(
        "action", "check" if request.path.endswith("/checks") else "preview"
    )
    valid = form.is_valid()
    report_options = (
        {name: form.cleaned_data[name] for name in ("report_type", "conclusion")}
        if valid else {}
    )
    if write and valid:
        request.session[f"report-comment-{obj.pk}"] = form.cleaned_data["comment"]
        request.session[f"report-options-{obj.pk}"] = report_options
        selected = list(form.cleaned_data["objects"].order_by("id"))
        if action == "show":
            return comparison_redirect(obj, selected, differences)
        if not check_version(request, obj):
            form.add_error(
                None, "Профиль изменился. Обновите страницу перед действием."
            )
        elif action == "check":
            try:
                for p in selected:
                    evaluate(p, request.user, obj)
                messages.success(request, "Проверка и расчёт сохранены.")
                return comparison_redirect(obj, selected, differences)
            except ValueError as exc:
                form.add_error(None, str(exc))
        elif action in ["parameter", "issue"] and str(
            obj.selection_version
        ) != request.POST.get("selection_version"):
            form.add_error(
                None, "Состав отчёта изменён в другой вкладке. Обновите страницу."
            )
        elif action == "parameter":
            key = request.POST.get("parameter")
            if key not in dict(choices):
                form.add_error(None, "Параметр недоступен для этого запроса.")
            else:
                parameters = set(obj.report_parameters)
                if key in parameters:
                    parameters.remove(key)
                else:
                    parameters.add(key)
                obj.report_parameters = sorted(parameters)
                obj.selection_version += 1
                obj.save(update_fields=["report_parameters", "selection_version"])
                audit(request.user, "report.parameters.updated", obj.pk)
                return comparison_redirect(obj, selected, differences)
        elif action == "issue":
            try:
                if not form.cleaned_data["creation_key"]:
                    raise ValueError("Обновите форму: отсутствует ключ отправки.")
                report = issue_report(
                    obj,
                    selected,
                    form.cleaned_data["comment"],
                    request.user,
                    form.cleaned_data["creation_key"],
                    parameters=obj.report_parameters,
                    **report_options,
                )
                messages.success(
                    request, f"Версия отчёта {report.version} сохранена."
                )
                return comparison_redirect(
                    obj, selected, differences, "report-versions"
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            except Exception as exc:
                import logging

                logging.getLogger(__name__).error(
                    "Report generation failed: %s", type(exc).__name__
                )
                form.add_error(
                    None,
                    "Не удалось сформировать отчёт. Ввод сохранён на экране; старые версии доступны ниже.",
                )
    if valid:
        selected = list(form.cleaned_data["objects"].order_by("id"))
        rows = compare_rows(selected, differences, request_obj=obj)
        for row in rows:
            criterion = next(
                (r for r in obj.requirements.all() if r.pk == row["requirement_id"]),
                None,
            )
            row["key"] = f"requirement:{criterion.pk}" if criterion else ""
            row["mandatory"] = criterion and criterion.priority == "required"
            row["included"] = row["mandatory"] or row["key"] in obj.report_parameters
        links = {
            link.premises_id: link
            for link in obj.premises_links.filter(premises__in=selected)
        }
        columns = []
        for i, p in enumerate(selected):
            ev = current_evaluation(p, obj)
            link = links.get(p.pk)
            costs = economics(p, obj, context=link)
            columns.append(
                {
                    "p": p,
                    "ev": ev,
                    "e": costs,
                    "outcome": (
                        ev.result["outcome"]
                        if ev
                        else assess_outcome(obj, p, "clarify", [], costs, link)
                    ),
                    "comparison_rows": [
                        {"name": r["name"], "cell": r["cells"][i]} for r in rows
                    ],
                }
            )
        try:
            preview = report_snapshot(
                obj,
                selected,
                form.cleaned_data["comment"],
                parameters=obj.report_parameters,
                **report_options,
            )
        except ValueError as exc:
            preview_error = str(exc)
    return render(
        request,
        "compare.html",
        {
            **tabs_context(request, obj, "compare"),
            "form": form,
            "selected": selected,
            "rows": rows,
            "columns": columns,
            "methods": METHODS,
            "insufficient": INSUFFICIENT,
            "regulatory_references": REFERENCES,
            "differences": differences,
            "preview": preview,
            "preview_error": preview_error,
            "parameter_choices": [
                {"key": key, "label": label, "included": key in obj.report_parameters}
                for key, label in choices
            ],
            "versions": obj.reports.defer("pdf", "snapshot")
            .select_related("author")
            .annotate(format_label=KeyTextTransform("format_label", "snapshot"))
            .order_by("-version"),
        },
    )


def comparison_url(obj, selected, differences=False):
    from urllib.parse import urlencode

    url = reverse("compare", args=[obj.pk])
    query = urlencode(
        [("objects", p.pk) for p in selected]
        + ([("differences", "1")] if differences else [])
    )
    return f"{url}?{query}" if query else url


def comparison_redirect(obj, selected, differences=False, anchor=""):
    url = comparison_url(obj, selected, differences)
    return redirect(url + ("#" + anchor if anchor else ""))


@workspace
@transaction.atomic
def checks(request, pk):
    obj = load_request(
        request, pk, write=request.method == "POST", lock=request.method == "POST"
    )
    if request.method == "POST":
        if not check_version(request, obj):
            messages.error(request, "Профиль изменился. Обновите страницу.")
            return redirect("compare", pk=pk)
        ids = request.POST.getlist("objects")
        try:
            selected = (
                list(obj.premises.filter(pk__in=ids))
                if ids
                else list(obj.premises.all())
            )
        except ValueError:
            raise Http404
        if ids and len(selected) != len(set(ids)):
            raise Http404
        try:
            for p in selected:
                evaluate(p, request.user, obj)
            messages.success(request, "Проверка и расчёт сохранены.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return comparison_redirect(obj, selected[:4])
    return redirect("compare", pk=pk)


@workspace
def reports(request, pk):
    load_request(request, pk, write=request.method == "POST")
    if request.method == "POST":
        return compare(request, pk)
    from urllib.parse import urlencode

    return redirect(
        reverse("compare", args=[pk])
        + ("?" + urlencode(request.GET, doseq=True) if request.GET else "")
        + "#report-builder"
    )


@workspace
@require_GET
def report_download(request, report_id):
    report = get_object_or_404(Report, pk=report_id)
    load_request(request, report.request_id)
    audit(request.user, "report.downloaded", report.pk)
    from io import BytesIO

    response = FileResponse(
        BytesIO(bytes(report.pdf)),
        as_attachment=True,
        filename=f"LeaseFIT-v{report.version}.pdf",
        content_type="application/pdf",
    )
    response["Cache-Control"] = "private, no-store"
    return response


@workspace
@transaction.atomic
def team(request):
    if request.member.role != "manager":
        raise PermissionDenied
    # Lock agency to serialize employee management.
    Agency.objects.select_for_update().get(pk=request.member.agency_id)
    form = InviteForm(
        request.POST
        if request.method == "POST" and request.POST.get("action") == "invite"
        else None
    )
    link = None
    if request.method == "POST":
        if request.POST.get("action") == "invite" and form.is_valid():
            email = form.cleaned_data["email"].lower()
            if User.objects.filter(username__iexact=email).exists():
                form.add_error(
                    "email", "Учётная запись уже существует. Обратитесь к оператору."
                )
            else:
                inv = Invitation.objects.filter(
                    agency=request.member.agency,
                    email__iexact=email,
                    accepted_at=None,
                    expires_at__gt=timezone.now(),
                ).first()
                if not inv:
                    inv, _ = Invitation.objects.get_or_create(
                        creation_key=form.cleaned_data["creation_key"],
                        defaults={
                            "agency": request.member.agency,
                            "email": email,
                            "created_by": request.user,
                            "expires_at": timezone.now() + timedelta(days=7),
                        },
                    )
                    if inv.agency_id != request.member.agency_id:
                        raise Http404
                    audit(request.user, "invitation.created", inv.pk)
                link = request.build_absolute_uri(reverse("activate", args=[inv.token]))
                messages.success(
                    request,
                    "Приглашение готово на 7 дней. Передайте ссылку сотруднику по согласованному каналу.",
                )
        elif request.POST.get("action") in ["deactivate", "reactivate"]:
            m = get_object_or_404(
                Membership,
                pk=request.POST.get("member_id"),
                agency=request.member.agency,
            )
            if m.pk == request.member.pk or m.role == "manager":
                messages.error(
                    request,
                    "Руководителя нельзя отключить здесь. Назначение замены выполняет оператор.",
                )
            else:
                m.active = request.POST["action"] == "reactivate"
                m.save()
                audit(request.user, "member." + request.POST["action"], m.pk)
                return redirect("team")
    return render(
        request,
        "team.html",
        {
            "form": form,
            "invite_link": link,
            "members": Membership.objects.filter(
                agency=request.member.agency
            ).select_related("user"),
            "invitations": Invitation.objects.filter(
                agency=request.member.agency
            ).order_by("-id")[:20],
        },
    )


@transaction.atomic
def activate(request, token):
    inv = get_object_or_404(Invitation.objects.select_for_update(), token=token)
    if not inv.valid:
        return render(request, "activation_invalid.html", status=410)
    user = User(username=inv.email.lower(), email=inv.email.lower())
    form = ActivateForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        if User.objects.filter(username__iexact=inv.email).exists():
            form.add_error(
                None, "Учётная запись уже существует. Обратитесь к руководителю."
            )
        else:
            user.first_name = form.cleaned_data["name"]
            form.save()
            Membership.objects.create(user=user, agency=inv.agency, role=inv.role)
            inv.accepted_at = timezone.now()
            inv.save()
            login(request, user)
            audit(user, "invitation.accepted", inv.pk)
            return redirect("requests")
    return render(request, "activate.html", {"form": form, "invitation": inv})


@transaction.atomic
def pilot(request):
    form = PilotForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        fingerprint = hashlib.sha256(
            "|".join(
                str(d[k]).strip().casefold()
                for k in ["agency", "city", "person", "contact"]
            ).encode()
        ).hexdigest()
        obj = (
            PilotApplication.objects.filter(creation_key=d["creation_key"]).first()
            or PilotApplication.objects.filter(fingerprint=fingerprint).first()
        )
        if not obj:
            defaults = {
                k: d[k]
                for k in [
                    "agency",
                    "city",
                    "person",
                    "contact",
                    "types",
                    "comment",
                    "consent",
                    "creation_key",
                ]
            }
            obj, _ = PilotApplication.objects.get_or_create(
                fingerprint=fingerprint, defaults=defaults
            )
        return render(request, "pilot_success.html")
    return render(request, "pilot.html", {"form": form})


@workspace
@require_GET
def settings_view(request):
    return render(request, "settings.html")


def privacy(request):
    return render(request, "privacy.html")


def example(request):
    return render(request, "example.html")


@require_GET
def healthz(request):
    from django.db import connection, DatabaseError

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


def filtered_catalog(request):
    from django.db.models import ExpressionWrapper, DecimalField
    from django.db.models.functions import Coalesce

    form = CatalogFilterForm(request.GET)
    qs = catalog_scope(request).order_by("-updated_at")
    if not form.is_valid():
        return qs.none(), form
    d = form.cleaned_data
    if d.get("q"):
        qs = qs.filter(label__icontains=d["q"])
    if d.get("premises_type"):
        qs = qs.filter(premises_type=d["premises_type"])
    if d.get("area_min") is not None:
        qs = qs.filter(area__gte=d["area_min"])
    if d.get("area_max") is not None:
        qs = qs.filter(area__lte=d["area_max"])
    if d.get("rent_max") is not None:
        qs = qs.annotate(
            base_rent=Coalesce(
                F("rent"),
                ExpressionWrapper(
                    F("area") * F("rate"),
                    output_field=DecimalField(max_digits=20, decimal_places=2),
                ),
            )
        ).filter(base_rent__lte=d["rent_max"])
    criteria = technical_filters(d)
    if criteria:
        ids = [
            p.pk
            for p in qs
            if all(check_requirement(r, p)["status"] == "pass" for r in criteria)
        ]
        qs = qs.filter(pk__in=ids)
    return qs, form


@workspace
@require_GET
def all_premises(request):
    qs, form = filtered_catalog(request)
    cards = [{"p": p, "economics": economics(p)} for p in qs]
    return render(
        request,
        "all_premises.html",
        {"cards": cards, "filter_form": form, "section": "all_premises"},
    )


@workspace
def start_premises(request):
    return premises_edit(request)


@workspace
@transaction.atomic
def select_premises(request, pk):
    req = load_request(
        request, pk, write=request.method == "POST", lock=request.method == "POST"
    )
    qs, filters = filtered_catalog(request)

    class AttachForm(forms.Form):
        objects = forms.ModelMultipleChoiceField(
            label="Выберите помещения",
            queryset=(
                catalog_scope(request).select_for_update().order_by("pk")
                if request.method == "POST"
                else catalog_scope(request)
            ),
        )

        def clean_objects(self):
            selected = self.cleaned_data["objects"]
            if selected.count() > 100:
                raise forms.ValidationError(
                    "За один раз можно добавить не больше 100 помещений."
                )
            return selected

    form = AttachForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        attach_selected(req, form.cleaned_data["objects"], request.user)
        messages.success(request, "Помещения добавлены без создания копий.")
        return redirect("objects", pk=pk)
    return render(
        request,
        "select_premises.html",
        {
            **tabs_context(request, req, "objects"),
            "cards": [{"p": p, "economics": economics(p)} for p in qs],
            "filter_form": filters,
            "selection_form": form,
            "attached_ids": list(req.premises.values_list("id", flat=True)),
        },
    )


def attach_selected(req, premises, actor):
    added = False
    for obj in premises:
        _, created = RequestPremises.objects.get_or_create(request=req, premises=obj)
        added = added or created
    if added:
        req.selection_version += 1
        req.save(update_fields=["selection_version"])
        audit(actor, "request.premises.attached", req.pk)
    return added


@workspace
@require_POST
@transaction.atomic
def attach_recommendation(request, pk, object_id):
    req = load_request(request, pk, write=True, lock=True)
    obj = get_object_or_404(catalog_scope(request).select_for_update(), pk=object_id)
    if not check_version(request, req) or request.POST.get("premises_version") != str(
        obj.version
    ):
        messages.warning(
            request,
            "Профиль или помещение изменились. Проверьте обновлённую рекомендацию.",
        )
    elif attach_selected(req, [obj], request.user):
        messages.success(request, "Помещение добавлено к сравнению.")
    else:
        messages.info(request, "Помещение уже в сравнении.")
    url = reverse("objects", args=[pk])
    query = request.GET.urlencode()
    return redirect(f"{url}?{query}" if query else url)


@workspace
@require_POST
@transaction.atomic
def detach_premises(request, pk, object_id):
    req = load_request(request, pk, write=True, lock=True)
    link = get_object_or_404(
        RequestPremises,
        request=req,
        premises_id=object_id,
        premises__agency=request.member.agency,
    )
    link.delete()
    req.selection_version += 1
    req.save(update_fields=["selection_version"])
    audit(request.user, "request.premises.detached", object_id)
    messages.success(
        request, "Связь убрана. Карточка, история проверок и PDF сохранены."
    )
    return redirect("objects", pk=pk)


@workspace
@require_POST
@transaction.atomic
def apply_business_template(request, pk):
    req = load_request(request, pk, write=True, lock=True)
    templates = {
        "Кофейня": ["water_supply", "sewerage", "ventilation", "separate_exhaust"],
        "ПВЗ": ["separate_entrance", "unloading_methods", "visitor_entry"],
        "Магазин": ["display_windows", "unloading_methods", "visitor_entry"],
        "Салон красоты": ["water_supply", "sewerage", "ventilation"],
        "Офис": ["visitor_entry", "heating", "air_conditioning"],
    }
    business = request.POST.get("business")
    if (
        business not in templates
        or not request.POST.get("confirm_template")
        or not check_version(request, req)
    ):
        messages.error(
            request,
            "Выберите пример и подтвердите добавление. Если профиль изменился, обновите страницу.",
        )
        return redirect("profile", pk=pk)
    changed = False
    for code in templates[business]:
        if not req.requirements.filter(
            characteristic__code__in={code, *LEGACY_BY_REPLACEMENT.get(code, ())}
        ).exists():
            char = Characteristic.objects.get(code=code)
            Requirement.objects.create(
                request=req,
                characteristic=char,
                name=char.label,
                unit=char.unit,
                operator="eq",
                priority="desired",
                structured_value=GENERAL_ACCESS.get(code),
                origin="legacy-template",
            )
            changed = True
    if changed:
        req.version += 1
        req.save()
        audit(request.user, "profile.template.added", req.pk)
    messages.success(
        request,
        "Добавлены только примерные желательные условия, которых ещё не было. Значения мощности и нормы не подставляются; ваши требования сохранены.",
    )
    return redirect("profile", pk=pk)


def new_recommendations(req, format_code, answers):
    requested = recommendations(format_code, answers)
    existing = list(req.requirements.values_list("characteristic_id", "name"))
    ids = {char_id for char_id, _ in existing}
    names = {name for _, name in existing}
    return [
        (char, requested[char.code])
        for char in Characteristic.objects.filter(code__in=requested).order_by("id")
        if char.pk not in ids and char.label not in names
    ]


@workspace
@transaction.atomic
def financial_plan(request, pk):
    write = request.method == "POST"
    req = load_request(request, pk, write=write, lock=write)
    form = FinancialPlanForm(request.POST or None, request_obj=req)
    context = {**tabs_context(request, req, "profile"), "form": form}
    if write and form.is_valid():
        if not check_version(request, req):
            return conflict(request, "financial_plan.html", context, form)
        plan = form.plan()
        if plan != req.financial_plan:
            req.financial_plan = plan
            req.version += 1
            req.save(update_fields=["financial_plan", "version", "updated_at"])
            audit(request.user, "profile.finance.updated", req.pk)
        messages.success(request, "Финансовый сценарий сохранён.")
        return redirect("compare", pk=pk)
    return render(request, "financial_plan.html", context)


@workspace
@transaction.atomic
def business_setup(request, pk):
    write = request.method == "POST"
    req = load_request(request, pk, write=write, lock=write)
    salt = "leasefit.business-template-v1"
    form = BusinessSetupForm(request.POST or None, request_obj=req)
    context = {**tabs_context(request, req, "profile"), "form": form}
    if write and request.POST.get("action") == "apply":
        try:
            payload = signing.loads(
                request.POST.get("preview", ""), salt=salt, max_age=1800
            )
        except signing.BadSignature:
            payload = {}
        if (
            payload.get("request") != req.pk
            or payload.get("version") != req.version
            or payload.get("format") not in FORMATS
        ):
            messages.error(
                request,
                "Профиль изменился или время предварительного просмотра истекло. Проверьте шаблон ещё раз.",
            )
            return redirect("business_setup", pk=pk)
        format_code = payload["format"]
        answers = payload["answers"]
        if (
            req.business_format != format_code
            and req.requirements.exists()
            and request.POST.get("confirm_category") != "yes"
        ):
            messages.error(
                request,
                "Подтвердите смену категории и сохранение существующих требований.",
            )
            return redirect("business_setup", pk=pk)
        additions = new_recommendations(req, format_code, answers)
        for char, value in additions:
            Requirement.objects.create(
                request=req,
                characteristic=char,
                name=char.label,
                unit=char.unit,
                operator="eq",
                priority="desired",
                structured_value=value if char.value_type in STRUCTURED else None,
                origin=f"{format_code}@{TEMPLATE_VERSION}",
            )
        if (
            additions
            or req.business_format != format_code
            or req.business_answers != answers
        ):
            req.business_format, req.business_answers = format_code, answers
            req.version += 1
            req.save(
                update_fields=[
                    "business_format",
                    "business_answers",
                    "version",
                    "updated_at",
                ]
            )
            audit(request.user, "profile.template.added", req.pk)
        messages.success(
            request, f"Формат сохранён. Добавлено рекомендаций: {len(additions)}."
        )
        return redirect("profile", pk=pk)
    if write and form.is_valid():
        format_code = form.cleaned_data["business_format"]
        answers = form.answers()
        context.update(
            choices=[
                (
                    char.label,
                    (
                        value_label(char.value_type, char.code, value, requirement=True)
                        if char.value_type in STRUCTURED
                        else "Наличие"
                    ),
                )
                for char, value in new_recommendations(req, format_code, answers)
            ],
            format_label=FORMATS[format_code].label,
            confirm_category=req.business_format != format_code
            and req.requirements.exists(),
            preview=signing.dumps(
                {
                    "request": req.pk,
                    "version": req.version,
                    "format": format_code,
                    "answers": answers,
                },
                salt=salt,
            ),
        )
    return render(request, "business_setup.html", context)


class MetrapiSearchForm(forms.Form):
    locality = forms.CharField(label="Город", initial="Новосибирск", max_length=120)
    query = forms.CharField(
        label="Адрес или текст объявления", required=False, max_length=250
    )
    area_min = forms.DecimalField(
        label="Площадь от, м²",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    area_max = forms.DecimalField(
        label="Площадь до, м²",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    limit = forms.TypedChoiceField(
        label="Объявлений в выдаче",
        choices=[(5, "5"), (10, "10"), (20, "20")],
        coerce=int,
        initial=10,
    )

    def clean(self):
        data = super().clean()
        lo, hi = data.get("area_min"), data.get("area_max")
        if lo is not None and hi is not None and lo > hi:
            self.add_error("area_max", "Верхняя граница меньше нижней.")
        return data


METRAPI_IMPORT_SALT = "leasefit.metrapi.import"


class MetrapiListingForm(forms.Form):
    url = forms.URLField(
        label="Ссылка на объявление",
        max_length=1500,
        assume_scheme="https",
        help_text="Авито, Циан, Домклик или Яндекс Недвижимость.",
    )

    def clean_url(self):
        try:
            return listing_reference(self.cleaned_data["url"])
        except MetrapiError as exc:
            raise forms.ValidationError(str(exc)) from None


def parser_member(user):
    member = getattr(user, "membership", None)
    if not user.is_superuser and not (
        member and member.active and member.role == "manager"
    ):
        raise PermissionDenied
    return member if member and member.active else None


@login_required
@require_http_methods(["GET", "POST"])
def parser_preview(request):
    member = parser_member(request.user)
    data = request.POST if request.method == "POST" else None
    lookup = data is not None and data.get("mode") == "listing"
    form = MetrapiSearchForm(None if lookup else data)
    listing_form = MetrapiListingForm(data if lookup else None)
    active_form = listing_form if lookup else form
    results = None
    imported = []
    if data is not None and active_form.is_valid():
        try:
            if lookup:
                results = [find_rental(listing_form.cleaned_data["url"])]
            else:
                results = search_rentals(**form.cleaned_data)
        except MetrapiError as exc:
            active_form.add_error(None, str(exc))
        else:
            if member:
                imported = import_listings(member.agency_id, request.user, results)
                by_source = {
                    (row["publication"].source, row["publication"].source_id): row
                    for row in imported
                }
                for item in results:
                    saved = by_source.get((item["source"], item["source_id"]))
                    if saved:
                        item["premises_id"] = saved["premises"].pk
                        item["publication_id"] = saved["publication"].pk
    return render(
        request,
        "parser.html",
        {
            "section": "parser",
            "form": form,
            "listing_form": listing_form,
            "lookup": lookup,
            "configured": bool(settings.METRAPI_API_KEY),
            "local_showcase": settings.LEASEFIT_SHOWCASE,
            "results": results,
            "can_import": member is not None,
            "imported_count": len({row["premises"].pk for row in imported if row["created"]}),
            "linked_count": len({row["publication"].pk for row in imported if row["linked"]}),
            "saved_count": len(imported),
        },
    )


@login_required
@require_POST
def parser_import(request):
    member = parser_member(request.user)
    if member is None:
        raise PermissionDenied("Для импорта необходимо активное агентство.")
    try:
        payload = signing.loads(
            request.POST.get("listing", ""),
            salt=METRAPI_IMPORT_SALT,
            max_age=1800,
        )
    except signing.BadSignature:
        messages.error(
            request,
            "Предпросмотр устарел или недействителен. Найдите объявление снова.",
        )
        return redirect("parser")
    if payload["user"] != request.user.pk or payload["agency"] != member.agency_id:
        raise PermissionDenied

    rows = import_listings(member.agency_id, request.user, [payload["item"]])
    if not rows:
        return redirect("parser")
    return redirect("catalog_premises", object_id=rows[0]["premises"].pk)


@workspace
@require_POST
def refresh_premises_listing(request, object_id, pk=None):
    parser_member(request.user)
    req, p = property_load(request, pk, object_id, write=True)
    reference = premises_listing_reference(p)
    if reference is None:
        raise Http404
    publication = p.publications.filter(source=reference[0], source_id=reference[1]).first()
    try:
        item = find_rental(reference)
    except ListingUnavailable as exc:
        if publication:
            refresh_publication(publication.pk, publication.checked_at, None, request.user)
        messages.warning(request, str(exc))
        return property_redirect(req, p)
    except MetrapiError as exc:
        messages.error(request, str(exc))
        return property_redirect(req, p)
    if publication:
        changed = refresh_publication(publication.pk, publication.checked_at, item, request.user)
    else:
        changed = bool(import_listings(request.member.agency_id, request.user, [item], target_id=p.pk))
    messages.success(
        request,
        "Материалы объявления обновлены." if changed else "Материалы объявления актуальны.",
    )
    return property_redirect(req, p)


@workspace
@require_GET
def publication_history(request, object_id, publication_id):
    p = get_object_or_404(catalog_scope(request), pk=object_id)
    publication = get_object_or_404(p.publications, pk=publication_id)
    page = Paginator(publication.versions.all(), 12).get_page(request.GET.get("page"))
    facts = dict(
        Characteristic.objects.filter(agency__isnull=True).values_list("code", "label")
    )
    versions = []
    for version in page:
        snapshot = version.snapshot
        rows = [
            (label, display_value(snapshot[key]))
            for key, label in SNAPSHOT_FIELDS.items()
            if key not in {"state", "description", "photos", "facts", "url", "group_id", "group_spread_m"}
            and snapshot.get(key) not in {None, "", "unknown"}
        ]
        for code, fact in (snapshot.get("facts") or {}).items():
            if code in {"area_sqm", "floor"}:
                continue
            value = fact.get("value", fact.get("text", fact.get("boolean")))
            rows.append((facts.get(code, code), value))
        versions.append({"entry": version, "fields": rows})
    return render(request, "publication_history.html", {
        "section": "all_premises", "p": p, "publication": publication,
        "title": publication_label(publication), "page": page, "versions": versions,
    })


@workspace
@transaction.atomic
def refine(request, pk):
    obj = load_request(
        request, pk, write=request.method == "POST", lock=request.method == "POST"
    )
    form = RefinementForm(request.POST or None, request_obj=obj)
    if request.method == "POST" and form.is_valid():
        if not check_version(request, obj):
            return conflict(
                request,
                "refine.html",
                {**tabs_context(request, obj, "refine"), "form": form},
                form,
            )
        d = form.cleaned_data
        changed = obj.budget != d["budget"] or obj.districts != d["districts"]
        obj.budget, obj.districts = d["budget"], d["districts"]
        specs = []
        if d.get("area_min") is not None or d.get("area_max") is not None:
            lo, hi = d.get("area_min"), d.get("area_max")
            specs.append(
                dict(
                    name="Площадь",
                    unit="м²",
                    operator=(
                        "range"
                        if lo is not None and hi is not None
                        else "gte" if lo is not None else "lte"
                    ),
                    minimum=lo if lo is not None else hi,
                    maximum=hi if lo is not None else None,
                    priority=d["area_priority"],
                )
            )
        if d.get("power") is not None:
            specs.append(
                dict(
                    name="Мощность",
                    unit="кВт",
                    operator="gte",
                    minimum=d["power"],
                    priority=d["power_priority"],
                )
            )
        for key, name in [("water", "Вода"), ("ventilation", "Отдельная вытяжка")]:
            if d.get(key):
                specs.append(
                    dict(
                        name=name,
                        unit="",
                        operator="eq",
                        expected=True,
                        priority="na" if d[key] == "na" else d[key + "_priority"],
                    )
                )
        for spec in specs:
            char = resolve_characteristic(
                obj.agency_id,
                spec["name"],
                spec["unit"],
                "boolean" if spec["operator"] == "eq" else "number",
            )
            Requirement.objects.create(request=obj, characteristic=char, **spec)
            changed = True
        if changed:
            obj.version += 1
            obj.save()
            audit(request.user, "profile.refined", obj.pk)
        return redirect("objects", pk=pk)
    return render(
        request, "refine.html", {**tabs_context(request, obj, "refine"), "form": form}
    )
