import hashlib, json
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from .models import *

RULES_VERSION = "p0-1.0"
LABELS = {
    "blocker": "Есть подтверждённый блокер",
    "clarify": "Требует уточнения",
    "pass": "Можно рассматривать",
    "stale": "Не проверено — нужен пересчёт",
}
ROW_LABELS = {
    "blocker": "Блокер",
    "clarify": "Уточнить",
    "pass": "Выполнено",
    "desired_fail": "Желательное не выполнено",
}


def money_text(value):
    value = Decimal(str(value))
    places = 0 if value == value.to_integral() else 2
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",") + " ₽"


def serial(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def audit(user, action, resource):
    AuditEvent.objects.create(
        actor=user, agency=user.membership.agency, action=action, resource=str(resource)
    )


def source_snapshot(o):
    return {
        "id": o.pk,
        "value": str(o.value)
        if o.value is not None
        else ("да" if o.boolean else "нет")
        if o.boolean is not None
        else o.text or "неизвестно",
        "unit": o.unit,
        "scope": o.get_scope_display(),
        "locator": o.locator,
        "source": o.source,
        "url": o.source_url,
        "date": str(o.observed_on),
        "type": o.get_source_type_display(),
        "status": o.get_status_display(),
        "confirmation": o.confirmation,
        "superseded": o.superseded,
    }


def check_requirement(req, p):
    observations = list(p.observations.filter(name__iexact=req.name).order_by("id"))
    live = [o for o in observations if not o.superseded]
    sources = [source_snapshot(o) for o in observations]
    reason = "Нет актуального подтверждённого значения для помещения."
    status = "clarify"
    comparable = [
        o for o in live if o.scope == "premises" and o.source_type != "archive"
    ]
    values = {
        (o.value if req.operator != "eq" else o.boolean, o.unit)
        for o in comparable
        if (o.value is not None if req.operator != "eq" else o.boolean is not None)
    }
    conflict = any(o.status == "conflict" for o in live) or len(values) > 1
    if conflict:
        reason = "Источники противоречат друг другу. Выберите актуальный источник и запишите основание."
    else:
        confirmed = [
            o
            for o in comparable
            if o.status == "confirmed"
            and o.source_type != "listing"
            and o.confirmation
            and o.unit == req.unit
            and (o.value is not None if req.operator != "eq" else o.boolean is not None)
        ]
        if confirmed:
            o = confirmed[-1]
            good = (
                (o.boolean == req.expected)
                if req.operator == "eq"
                else (
                    o.value >= req.minimum
                    if req.operator == "gte"
                    else o.value <= req.minimum
                    if req.operator == "lte"
                    else req.minimum <= o.value <= req.maximum
                )
            )
            status = (
                "pass"
                if good
                else ("blocker" if req.priority == "required" else "desired_fail")
            )
            reason = (
                "Подтверждённое значение соответствует требованию."
                if good
                else "Подтверждённое значение не соответствует требованию."
            )
        elif live:
            reason = "Значение не подтверждено для всего помещения в текущем состоянии. Архив, объявление, комната и здание требуют уточнения; единицы должны совпадать."
    question = (
        f"Подтвердите для этого помещения: {req}. Укажите актуальный документ или ответ собственника, дату и единицу."
        if status == "clarify"
        else (
            f"Возможно ли изменить условия, чтобы выполнить: {req}?"
            if status in ["blocker", "desired_fail"]
            else ""
        )
    )
    return {
        "requirement": str(req),
        "name": req.name,
        "priority": req.priority,
        "status": status,
        "label": ROW_LABELS[status],
        "reason": reason,
        "question": question,
        "sources": sources,
        "value": "; ".join(
            f"{s['value']} {s['unit']} ({s['scope']}, {s['status']}, {s['date']})"
            for s in sources
        )
        or "Неизвестно",
    }


def economics(p):
    zero = Decimal("0")
    missing = []
    monthly = []
    calculated = p.rate * p.area if p.rate is not None and p.area is not None else None
    conflict = (
        p.rent is not None
        and calculated is not None
        and abs(p.rent - calculated) > Decimal(".01")
    )
    base = p.rent if p.rent is not None else calculated
    if base is None:
        missing.append("Базовая аренда / площадь для ставки")
    if conflict:
        missing.append("Конфликт месячной аренды и ставки × площади")
    monthly.append(("Базовая аренда", base))
    for field in ["operating", "utilities", "other_monthly"]:
        v = getattr(p, field)
        monthly.append((p._meta.get_field(field).verbose_name, v))
        if v is None:
            missing.append(str(p._meta.get_field(field).verbose_name))
    vat = None
    if p.vat_mode == "unknown":
        missing.append("Условия НДС")
    elif p.vat_mode == "excluded":
        if p.vat_rate is None or base is None:
            missing.append("НДС: ставка и база")
        else:
            vat = (base * p.vat_rate / 100).quantize(Decimal(".01"))
    else:
        vat = zero
    monthly.append(("НДС сверх базы", vat))
    total = sum((v for _, v in monthly if v is not None), zero).quantize(Decimal(".01"))
    once = []
    once_missing = []
    for field in ["deposit", "commission", "adaptation", "other_once"]:
        v = getattr(p, field)
        once.append((p._meta.get_field(field).verbose_name, v))
        if v is None:
            once_missing.append(str(p._meta.get_field(field).verbose_name))
    once_total = sum((v for _, v in once if v is not None), zero)
    budget = p.request.budget
    gap = total - budget if budget is not None else None
    budget_label = (
        "Лимит неизвестен"
        if budget is None
        else (
            "Превышение " + money_text(gap)
            if gap > 0
            else "Известный подытог ниже лимита; итог не определён"
            if missing
            else "В пределах месячного лимита"
        )
    )
    others = [p.operating, p.utilities, p.other_monthly]
    allowed = None
    if (
        budget is not None
        and all(x is not None for x in others)
        and p.vat_mode != "unknown"
        and (p.vat_mode != "excluded" or p.vat_rate is not None)
    ):
        remainder = budget - sum(others, zero)
        allowed = (
            remainder / (1 + p.vat_rate / 100)
            if remainder > 0 and p.vat_mode == "excluded"
            else remainder
        ).quantize(Decimal(".01"))
    return serial(
        {
            "monthly": monthly,
            "monthly_total": total,
            "complete": not missing,
            "missing": missing,
            "base": base,
            "rent_conflict": conflict,
            "once": once,
            "once_total": once_total,
            "once_complete": not once_missing,
            "once_missing": once_missing,
            "deposit_refundable": p.deposit_refundable,
            "gap": gap,
            "budget_label": budget_label,
            "allowed_rent": allowed,
            "stress_total": total + p.stress,
            "stress_addition": p.stress,
            "start_gap": once_total - p.request.start_budget
            if p.request.start_budget is not None
            else None,
            "assumptions": p.assumptions,
            "commercial_evidence": {
                "source": p.commercial_source
                or "Источник не указан — расчёт по ручным вводным",
                "type": p.get_commercial_source_type_display(),
                "date": str(p.commercial_date)
                if p.commercial_date
                else "Дата неизвестна",
                "status": p.get_commercial_status_display(),
                "confirmation": p.commercial_confirmation,
                "scope": "Помещение / блок",
                "units": "₽; месяц; м²",
            },
        }
    )


def evaluate(p, user):
    if not p.request.ready:
        raise ValueError("Задайте хотя бы одно обязательное требование в профиле.")
    rows = [
        check_requirement(r, p)
        for r in p.request.requirements.exclude(priority="na").order_by("id")
    ]
    mandatory = [r for r in rows if r["priority"] == "required"]
    status = (
        "blocker"
        if any(r["status"] == "blocker" for r in mandatory)
        else "clarify"
        if any(r["status"] == "clarify" for r in mandatory)
        else "pass"
    )
    result = {
        "status": status,
        "label": LABELS[status],
        "rows": rows,
        "economics": economics(p),
    }
    latest = p.evaluations.order_by("-id").first()
    if (
        latest
        and latest.current
        and latest.rules_version == RULES_VERSION
        and latest.result == result
    ):
        return latest
    ev = Evaluation.objects.create(
        premises=p,
        profile_version=p.request.version,
        premises_version=p.version,
        result=result,
        author=user,
        rules_version=RULES_VERSION,
    )
    audit(user, "check.completed", p.pk)
    return ev


def current_evaluation(p):
    ev = p.evaluations.order_by("-id").first()
    return ev if ev and ev.current and ev.rules_version == RULES_VERSION else None


def report_snapshot(req, objects, comment):
    items = []
    identifiers = []
    for p in objects:
        identifier = " / ".join(
            filter(None, [p.label.strip(), p.floor.strip(), p.number.strip()])
        )
        if identifier.casefold() in identifiers:
            raise ValueError(
                "Выбранные варианты должны иметь разные однозначные метки. Уточните этаж / номер."
            )
        identifiers.append(identifier.casefold())
        ev = current_evaluation(p)
        if not ev:
            raise ValueError(
                f"{p.label}: оценка устарела или отсутствует. Пересчитайте перед выпуском PDF."
            )
        decision = {
            "pass": "Можно рассматривать с учётом экономики",
            "clarify": "Только условное рассмотрение после уточнений",
            "blocker": "Отсеян при текущих условиях",
        }[ev.result["status"]]
        items.append(
            {
                "id": p.pk,
                "title": identifier,
                "listing_url": p.listing_url,
                "evaluation_id": ev.pk,
                "checked_at": timezone.localtime(ev.created_at).isoformat(),
                "decision": decision,
                **ev.result,
            }
        )
    return serial(
        {
            "rules_version": RULES_VERSION,
            "request": req.title,
            "city": req.city,
            "business": req.business,
            "budget": req.budget,
            "start_budget": req.start_budget,
            "geography": req.geography,
            "profile_version": req.version,
            "requirements": [
                f"{r.get_priority_display()}: {r}"
                for r in req.requirements.exclude(priority="na")
            ],
            "objects": items,
            "comment": comment,
        }
    )


@transaction.atomic
def issue_report(req, objects, comment, user, key):
    req = ClientRequest.objects.select_for_update().get(pk=req.pk)
    if req.archived or req.stage == "done":
        raise ValueError("Верните запрос в работу перед выпуском отчёта.")
    old = Report.objects.filter(creation_key=key, request=req).first()
    if old:
        return old
    # All mutations lock the same parent request, preventing mixed-version snapshots.
    objects = list(req.premises.filter(pk__in=[p.pk for p in objects]).order_by("id"))
    if not 1 <= len(objects) <= 4:
        raise ValueError("Выберите от 1 до 4 вариантов.")
    snap = report_snapshot(req, objects, comment)
    fingerprint = hashlib.sha256(
        json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    existing = Report.objects.filter(request=req, fingerprint=fingerprint).first()
    if existing:
        return existing
    version = (
        req.reports.order_by("-version").values_list("version", flat=True).first() or 0
    ) + 1
    snap.update(
        version=version,
        issued_at=timezone.localtime(timezone.now()).isoformat(),
        author=user.get_full_name() or user.username,
        agency=req.agency.name,
    )
    from .pdf import render_pdf

    pdf = render_pdf(snap)
    report = Report.objects.create(
        request=req,
        version=version,
        snapshot=snap,
        pdf=pdf,
        fingerprint=fingerprint,
        creation_key=key,
        author=user,
    )
    audit(user, "report.issued", report.pk)
    return report


def compare_rows(objects, differences=False):
    # A missing evaluation is never presented as a current recommendation.
    evaluations = [current_evaluation(p) for p in objects]
    rows = []
    for i, req in (
        enumerate(objects[0].request.requirements.exclude(priority="na").order_by("id"))
        if objects
        else []
    ):
        cells = []
        critical = False
        for ev in evaluations:
            cell = (
                next((r for r in ev.result["rows"] if r["name"] == req.name), None)
                if ev
                else None
            )
            cells.append(
                cell
                or {
                    "label": LABELS["stale"],
                    "status": "stale",
                    "value": "Пересчитайте",
                    "reason": "Исходные данные изменились или проверка не выполнена.",
                    "sources": [],
                }
            )
            if req.priority == "required" and (
                not cell or cell["status"] in ["blocker", "clarify"]
            ):
                critical = True
        sigs = [(c["status"], c["value"]) for c in cells]
        if not differences or critical or len(set(sigs)) > 1:
            rows.append({"name": str(req), "cells": cells})
    return rows
