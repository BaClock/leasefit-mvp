import hashlib, json, uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, FileResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.core.exceptions import PermissionDenied
from .models import *
from .forms import *
from .services import *


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
    form = RequestForm(request.POST or None, instance=obj)
    formset = RequirementsFormSet(
        request.POST or None, instance=obj, prefix="requirements"
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
                return redirect("profile", pk=existing.pk)
        valid = form.is_valid() and formset.is_valid()
        ctx = {
            **tabs_context(request, obj, "profile"),
            "form": form,
            "formset": formset,
            "new": not pk,
        }
        if pk and not check_version(request, obj):
            return conflict(request, "profile.html", ctx, form)
        if valid:
            obj = form.save(commit=False)
            if not pk:
                obj.creation_key = form.cleaned_data["creation_key"]
            if pk:
                obj.version += 1
            obj.save()
            formset.instance = obj
            formset.save()
            audit(request.user, "profile.updated" if pk else "request.created", obj.pk)
            messages.success(
                request,
                "Профиль сохранён. Предыдущие оценки требуют пересчёта."
                if pk
                else "Запрос создан. Добавьте варианты помещений.",
            )
            return redirect("profile" if pk else "objects", pk=obj.pk)
    return render(
        request,
        "profile.html",
        {
            **tabs_context(request, obj, "profile"),
            "form": form,
            "formset": formset,
            "new": not pk,
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
@require_GET
def objects(request, pk):
    obj = load_request(request, pk)
    cards = []
    for p in obj.premises.all().order_by("id"):
        cards.append({"p": p, "ev": current_evaluation(p), "economics": economics(p)})
    return render(
        request,
        "objects.html",
        {**tabs_context(request, obj, "objects"), "cards": cards},
    )


@workspace
@transaction.atomic
def premises_edit(request, pk, object_id=None):
    obj = load_request(
        request, pk, write=request.method == "POST", lock=request.method == "POST"
    )
    p = (
        get_object_or_404(obj.premises, pk=object_id)
        if object_id
        else Premises(request=obj)
    )
    form = PremisesForm(request.POST or None, instance=p)
    obsform = ObservationForm(initial={"observed_on": timezone.localdate()})
    ctx = {
        **tabs_context(request, obj, "objects"),
        "p": p,
        "form": form,
        "obsform": obsform,
        "uploadform": AttachmentForm(),
        "new_object": not object_id,
    }
    if request.method == "POST":
        if not object_id:
            try:
                key = uuid.UUID(request.POST.get("creation_key", ""))
            except (ValueError, TypeError, AttributeError):
                key = None
            existing = obj.premises.filter(creation_key=key).first() if key else None
            if existing:
                return redirect("premises", pk=pk, object_id=existing.pk)
        valid = form.is_valid()
        if object_id and not check_version(request, p):
            return conflict(request, "premises.html", ctx, form)
        if valid:
            p = form.save(commit=False)
            if not object_id:
                p.creation_key = form.cleaned_data["creation_key"]
            if object_id:
                p.version += 1
            p.save()
            duplicate = (
                obj.premises.filter(
                    label__iexact=p.label,
                    floor__iexact=p.floor,
                    number__iexact=p.number,
                )
                .exclude(pk=p.pk)
                .exists()
            )
            if duplicate:
                messages.warning(
                    request,
                    "Возможный дубль: адрес / метка, этаж и номер совпадают с другой карточкой. Записи сохранены раздельно.",
                )
            audit(
                request.user,
                "premises.updated" if object_id else "premises.created",
                p.pk,
            )
            messages.success(
                request,
                "Карточка сохранена. Добавьте сведения с источниками и запустите проверку.",
            )
            return redirect("premises", pk=pk, object_id=p.pk)
    if p.pk:
        ctx.update(
            observations=p.observations.order_by("name", "id"),
            ev=current_evaluation(p),
            files=p.attachments.all(),
            economics=economics(p),
        )
    return render(request, "premises.html", ctx)


def detail_with_errors(request, obj, p, obsform=None, uploadform=None, status=400):
    return render(
        request,
        "premises.html",
        {
            **tabs_context(request, obj, "objects"),
            "p": p,
            "form": PremisesForm(instance=p),
            "obsform": obsform
            or ObservationForm(initial={"observed_on": timezone.localdate()}),
            "uploadform": uploadform or AttachmentForm(),
            "observations": p.observations.order_by("name", "id"),
            "files": p.attachments.all(),
            "ev": current_evaluation(p),
            "economics": economics(p),
        },
        status=status,
    )


@workspace
@require_POST
@transaction.atomic
def add_observation(request, pk, object_id):
    obj = load_request(request, pk, write=True, lock=True)
    p = get_object_or_404(obj.premises, pk=object_id)
    form = ObservationForm(request.POST)
    if form.is_valid():
        existing = p.observations.filter(
            creation_key=form.cleaned_data["creation_key"]
        ).first()
        if existing:
            return redirect("premises", pk=pk, object_id=object_id)
        if not check_version(request, p):
            form.add_error(
                None, "Карточка изменилась. Обновите её перед добавлением сведений."
            )
            return detail_with_errors(request, obj, p, obsform=form, status=409)
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
            "Сведения сохранены. Если источники расходятся, выберите актуальный ниже и объясните выбор.",
        )
        return redirect("premises", pk=pk, object_id=object_id)
    return detail_with_errors(request, obj, p, obsform=form)


@workspace
@require_POST
@transaction.atomic
def resolve(request, pk, object_id, observation_id):
    obj = load_request(request, pk, write=True, lock=True)
    p = get_object_or_404(obj.premises, pk=object_id)
    o = get_object_or_404(p.observations, pk=observation_id)
    reason = request.POST.get("reason", "").strip()
    if not check_version(request, p):
        messages.error(request, "Карточка изменилась. Обновите страницу.")
        return redirect("premises", pk=pk, object_id=object_id)
    if (
        not reason
        or len(reason) > 700
        or o.superseded
        or o.source_type in ["archive", "listing"]
        or o.scope != "premises"
        or (o.value is None and o.boolean is None and not o.text)
    ):
        messages.error(
            request,
            "Нужны значение для помещения, актуальный документ / ответ и основание выбора (до 700 знаков). Архив и объявление не подтверждают текущее значение.",
        )
    else:
        p.observations.filter(name__iexact=o.name).exclude(pk=o.pk).update(
            superseded=True
        )
        o.status = "confirmed"
        o.confirmation = reason
        o.save()
        p.version += 1
        p.save()
        audit(request.user, "conflict.resolved", o.pk)
        messages.success(
            request,
            "Выбран актуальный источник. История сохранена. Пересчитайте оценку.",
        )
    return redirect("premises", pk=pk, object_id=object_id)


@workspace
@require_POST
@transaction.atomic
def upload(request, pk, object_id):
    obj = load_request(request, pk, write=True, lock=True)
    p = get_object_or_404(obj.premises, pk=object_id)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        if not check_version(request, p):
            form.add_error(
                None, "Карточка изменилась. Обновите страницу и выберите файл заново."
            )
            return detail_with_errors(request, obj, p, uploadform=form, status=409)
        f = form.cleaned_data["file"]
        data = f.read()
        digest = hashlib.sha256(data).hexdigest()
        attachment, created = Attachment.objects.get_or_create(
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
            audit(request.user, "attachment.uploaded", attachment.pk)
        messages.success(
            request,
            "Файл сохранён в закрытом хранилище."
            if created
            else "Этот файл уже есть в карточке.",
        )
        return redirect("premises", pk=pk, object_id=object_id)
    return detail_with_errors(request, obj, p, uploadform=form)


@workspace
@require_GET
def attachment_download(request, file_id):
    f = get_object_or_404(
        Attachment.objects.select_related("premises__request"), pk=file_id
    )
    load_request(request, f.premises.request_id)
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
def checks(request, pk):
    obj = load_request(
        request, pk, write=request.method == "POST", lock=request.method == "POST"
    )
    if request.method == "POST":
        if not check_version(request, obj):
            messages.error(request, "Профиль изменился. Обновите страницу.")
            return redirect("checks", pk=pk)
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
                evaluate(p, request.user)
            messages.success(
                request,
                "Проверка и расчёт сохранены."
                if selected
                else "Добавьте хотя бы одно помещение.",
            )
        except ValueError as e:
            messages.error(request, str(e))
        return redirect("checks", pk=pk)
    cards = [
        {"p": p, "ev": current_evaluation(p)} for p in obj.premises.all().order_by("id")
    ]
    return render(
        request, "checks.html", {**tabs_context(request, obj, "checks"), "cards": cards}
    )


@workspace
@require_GET
def compare(request, pk):
    obj = load_request(request, pk)
    form = SelectionForm(
        request.GET if "objects" in request.GET else None,
        request_obj=obj,
        minimum=2,
        initial={"objects": list(obj.premises.values_list("id", flat=True)[:4])},
    )
    selected = []
    rows = []
    columns = []
    differences = request.GET.get("differences") == "1"
    if form.is_bound and form.is_valid():
        selected = list(form.cleaned_data["objects"].order_by("id"))
        rows = compare_rows(selected, differences)
        columns = [
            {
                "p": p,
                "ev": current_evaluation(p),
                "e": economics(p),
                "comparison_rows": [
                    {"name": r["name"], "cell": r["cells"][i]} for r in rows
                ],
            }
            for i, p in enumerate(selected)
        ]
        audit(request.user, "compare.opened", obj.pk)
    return render(
        request,
        "compare.html",
        {
            **tabs_context(request, obj, "compare"),
            "form": form,
            "selected": selected,
            "rows": rows,
            "columns": columns,
            "differences": differences,
            "selected_query": "&".join("objects=" + str(p.pk) for p in selected),
        },
    )


@workspace
def reports(request, pk):
    obj = load_request(request, pk, write=request.method == "POST")
    data = (
        request.POST
        if request.method == "POST"
        else request.GET
        if "objects" in request.GET
        else None
    )
    if request.method == "GET" and data is not None:
        data = data.copy()
        data["creation_key"] = str(uuid.uuid4())
    form = SelectionForm(
        data,
        request_obj=obj,
        initial={"objects": list(obj.premises.values_list("id", flat=True)[:4])},
    )
    preview = None
    if data is not None and form.is_valid():
        selected = list(form.cleaned_data["objects"].order_by("id"))
        try:
            preview = report_snapshot(obj, selected, form.cleaned_data["comment"])
            if request.method == "POST" and not form.cleaned_data["creation_key"]:
                raise ValueError("Обновите форму: отсутствует ключ отправки.")
            if request.method == "POST" and request.POST.get("action") == "issue":
                report = issue_report(
                    obj,
                    selected,
                    form.cleaned_data["comment"],
                    request.user,
                    form.cleaned_data["creation_key"],
                )
                messages.success(
                    request, f"Клиентская версия {report.version} сохранена."
                )
                return redirect("reports", pk=pk)
        except ValueError as e:
            form.add_error(None, str(e))
        except Exception as exc:
            import logging

            logging.getLogger(__name__).error(
                "Report generation failed for request %s: %s",
                obj.pk,
                type(exc).__name__,
            )
            form.add_error(
                None,
                "Не удалось сохранить PDF. Введённые данные сохранены на экране; повторите отправку. Старые версии доступны ниже.",
            )
    return render(
        request,
        "reports.html",
        {
            **tabs_context(request, obj, "reports"),
            "form": form,
            "preview": preview,
            "versions": obj.reports.defer("pdf", "snapshot").order_by("-version"),
        },
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
