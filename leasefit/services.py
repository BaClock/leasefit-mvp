import hashlib, json
from collections import Counter, defaultdict
from decimal import Decimal
from django.db import transaction
from django.db.models import Prefetch, Q
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import *
from .operations import (
    LEGACY_BY_REPLACEMENT,
    STRUCTURED,
    matches,
    normalize_value,
    value_label,
    weekly_coverage,
)
from .metrics import financial_metrics, evidence_summary, opening_costs
from .regulations import regulatory_checklist
from .geography import district_description
from .outcomes import assess_outcome, budget_excess, money_text, outcome_priority
from .report_content import (
    BROKER_CONCLUSIONS,
    BROKER_DETAILED,
    CLIENT_SECTIONS,
    CLIENT_SHORT,
    REPORT_FORMATS,
    client_object,
    client_summary,
    public_text,
)

RULES_VERSION = "p0-3.2"
LOAD_LINK = object()
LABELS = {
    "blocker": "Не соответствует · подтверждённый блокер",
    "clarify": "Требует уточнения",
    "pass": "Соответствует",
    "stale": "Не проверено — нужен пересчёт",
}
ROW_LABELS = {
    "blocker": "Не соответствует · Блокер",
    "clarify": "Требует уточнения",
    "pass": "Соответствует",
    "desired_fail": "Желательное не выполнено",
}


def serial(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def audit(user, action, resource):
    AuditEvent.objects.create(
        actor=user, agency=user.membership.agency, action=action, resource=str(resource)
    )


def source_snapshot(o, structured_label=None):
    return {
        "id": o.pk,
        "value": (
            structured_label
            if structured_label is not None
            else (
                str(o.value)
                if o.value is not None
                else (
                    ("да" if o.boolean else "нет")
                    if o.boolean is not None
                    else o.text or "неизвестно"
                )
            )
        ),
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
        "has_value": (
            structured_label != "неизвестно"
            if structured_label is not None
            else o.value is not None or o.boolean is not None or bool(o.text.strip())
        ),
    }


def normalized_text(code, value):
    value = " ".join(value.strip().casefold().split())
    if code == "floor":
        return {
            "первый этаж": "1",
            "1-й этаж": "1",
            "1 этаж": "1",
            "второй этаж": "2",
            "2 этаж": "2",
            "подвал": "basement",
            "цоколь": "ground",
        }.get(value, value)
    return value


def check_requirement(req, p, *, observations=None):
    if observations is None:
        observations = list(
            p.observations.filter(characteristic_id=req.characteristic_id).order_by(
                "id"
            )
        )
    kind = req.characteristic.value_type
    if kind in STRUCTURED:
        return check_structured_requirement(req, observations)
    live = [o for o in observations if not o.superseded]
    sources = [source_snapshot(o) for o in observations]
    reason = "Нет актуального подтверждённого значения для помещения."
    status = "clarify"
    adjustment = ""
    comparable = [
        o for o in live if o.scope == "premises" and o.source_type != "archive"
    ]
    values = {
        (
            (
                o.value
                if kind == "number"
                else o.boolean if kind == "boolean" else o.text.strip().casefold()
            ),
            o.unit,
        )
        for o in comparable
        if (
            o.value is not None
            if kind == "number"
            else o.boolean is not None if kind == "boolean" else bool(o.text.strip())
        )
    }
    conflict = any(o.status == "conflict" for o in live) or len(values) > 1
    invalid_rule = (
        req.operator not in req.characteristic.allowed_operators
        or (kind == "text" and not req.expected_text)
        or (
            kind == "number"
            and (
                req.minimum is None or (req.operator == "range" and req.maximum is None)
            )
        )
        or req.unit != req.characteristic.unit
    )
    if invalid_rule:
        reason = "Уточните тип, единицу или значение требования в профиле. Прежние сведения сохранены без автоматического толкования."
    elif conflict:
        reason = "Источники противоречат друг другу. Выберите актуальный источник и запишите основание."
    else:
        confirmed = [
            o
            for o in comparable
            if o.status == "confirmed"
            and o.source_type != "listing"
            and o.confirmation
            and o.unit == req.unit == req.characteristic.unit
            and (
                o.value is not None
                if kind == "number"
                else (
                    o.boolean is not None if kind == "boolean" else bool(o.text.strip())
                )
            )
        ]
        if confirmed:
            o = confirmed[-1]
            good = (
                (o.boolean == req.expected)
                if kind == "boolean"
                else (
                    normalized_text(req.characteristic.code, o.text)
                    == normalized_text(req.characteristic.code, req.expected_text)
                    if kind == "text"
                    else (
                        o.value == req.minimum
                        if req.operator == "eq"
                        else (
                            o.value >= req.minimum
                            if req.operator == "gte"
                            else (
                                o.value <= req.minimum
                                if req.operator == "lte"
                                else req.minimum <= o.value <= req.maximum
                            )
                        )
                    )
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
            if not good and kind == "number":
                ceiling = req.maximum if req.operator == "range" else req.minimum
                if req.operator in ("gte", "range", "eq") and o.value < req.minimum:
                    adjustment = f"Не хватает {req.minimum - o.value:g} {req.unit}."
                elif req.operator in ("lte", "range", "eq") and o.value > ceiling:
                    adjustment = f"Превышение на {o.value - ceiling:g} {req.unit}."
        elif live:
            reason = "Значение не подтверждено для всего помещения в текущем состоянии. Архив, объявление, комната и здание требуют уточнения; единицы должны совпадать."
    return requirement_result(req, status, reason, sources, adjustment)


def check_structured_requirement(req, observations):
    kind = req.characteristic.value_type
    code = req.characteristic.code
    sources = []
    available = []
    for observation in observations:
        try:
            value = normalize_value(kind, code, observation.structured_value)
        except ValidationError:
            value = None
        sources.append(
            source_snapshot(observation, value_label(kind, code, value) or "неизвестно")
        )
        if (
            value is not None
            and not observation.superseded
            and observation.scope == "premises"
            and observation.source_type != "archive"
        ):
            available.append((observation, value))

    try:
        expected = normalize_value(kind, code, req.structured_value, requirement=True)
    except ValidationError:
        return requirement_result(
            req, "clarify", "Укажите точное условие в профиле.", sources
        )
    if req.operator != "eq" or req.unit != req.characteristic.unit:
        return requirement_result(
            req, "clarify", "Уточните тип сравнения или единицу в профиле.", sources
        )
    signatures = {
        (
            tuple(weekly_coverage(value))
            if kind == "time_schedule"
            else tuple(value) if kind == "multi_select" else value
        )
        for _, value in available
    }
    if len(signatures) > 1 or any(
        o.status == "conflict" and not o.superseded for o in observations
    ):
        return requirement_result(
            req,
            "clarify",
            "Источники противоречат друг другу. Выберите актуальный источник.",
            sources,
        )
    for observation, value in reversed(available):
        if (
            observation.status == "confirmed"
            and observation.source_type != "listing"
            and observation.confirmation
        ):
            if matches(kind, expected, value):
                return requirement_result(
                    req,
                    "pass",
                    "Подтверждённые условия покрывают запрос клиента.",
                    sources,
                )
            status = "blocker" if req.priority == "required" else "desired_fail"
            return requirement_result(
                req,
                status,
                "Подтверждённые условия не покрывают запрос клиента.",
                sources,
            )
    return requirement_result(
        req,
        "clarify",
        "Запросите подтверждение конкретного режима для всего помещения. Прежнее общее «Да» не подтверждает график или способ доступа.",
        sources,
    )


def requirement_result(req, status, reason, sources, adjustment=""):
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
        "requirement_id": req.pk,
        "name": req.name,
        "priority": req.priority,
        "status": status,
        "label": ROW_LABELS[status],
        "characteristic_id": req.characteristic_id,
        "code": req.characteristic.code,
        "reason": reason,
        "adjustment": adjustment,
        "question": question,
        "sources": sources,
        "value": "; ".join(
            f"{s['value']} {s['unit']} ({s['scope']}, {s['status']}, {s['date']})"
            for s in sources
        )
        or "Неизвестно",
    }


def economics(p, request_obj=None, *, context=LOAD_LINK):
    if context is LOAD_LINK:
        context = (
            RequestPremises.objects.filter(premises=p, request=request_obj).first()
            if request_obj is not None
            else None
        )
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
    if p.commercial_status == "conflict":
        missing.append("Противоречивые коммерческие условия")
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
        holder = p if field in ["deposit", "commission"] else context
        v = getattr(holder, field, None)
        label_model = (
            Premises if field in ["deposit", "commission"] else RequestPremises
        )
        once.append((label_model._meta.get_field(field).verbose_name, v))
        if v is None:
            once_missing.append(str(label_model._meta.get_field(field).verbose_name))
    once_total = sum((v for _, v in once if v is not None), zero)
    budget = request_obj.budget if request_obj else None
    gap = total - budget if budget is not None else None
    budget_met = monthly_budget_met(
        p, gap, complete=not missing, rent_conflict=conflict
    )
    if budget is None:
        budget_label = "Лимит неизвестен"
    elif budget_met is False:
        budget_label = (
            "Известные расходы выше лимита на " if missing else "Превышение "
        ) + money_text(gap)
    elif budget_met is True:
        budget_label = "В пределах месячного лимита"
    elif gap > 0 and not conflict and p.commercial_status != "conflict":
        budget_label = (
            f"Превышение возможно. По введённым суммам разница {money_text(gap)}. "
            "Полная сумма требует уточнения"
        )
    else:
        budget_label = "Полная сумма и соблюдение лимита требуют уточнения"
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
            "stress_total": total + context.stress if context else None,
            "stress_addition": context.stress if context else None,
            "start_gap": (
                once_total - request_obj.start_budget
                if context is not None
                and request_obj
                and request_obj.start_budget is not None
                else None
            ),
            "assumptions": "\n".join(
                filter(None, [p.lease_terms, context.assumptions if context else ""])
            ),
            "commercial_evidence": {
                "source": p.commercial_source
                or "Источник не указан — расчёт по ручным вводным",
                "type": p.get_commercial_source_type_display(),
                "date": (
                    str(p.commercial_date) if p.commercial_date else "Дата неизвестна"
                ),
                "status": p.get_commercial_status_display(),
                "confirmation": p.commercial_confirmation,
                "scope": "Помещение / блок",
                "units": "₽; месяц; м²",
            },
        }
    )


def monthly_budget_met(p, gap, *, complete, rent_conflict):
    if gap is None or rent_conflict or p.commercial_status == "conflict":
        return None
    if complete:
        return Decimal(gap) <= 0
    if Decimal(gap) > 0 and p.commercial_status == "confirmed":
        return False  # A confirmed subtotal alone exceeds the limit.
    return None


def budget_result(p, costs):
    return monthly_budget_met(
        p,
        costs["gap"],
        complete=costs["complete"],
        rent_conflict=costs["rent_conflict"],
    )


def active_requirements(req):
    requirements = list(
        req.requirements.exclude(priority="na")
        .select_related("characteristic")
        .order_by("id")
    )
    replacements = {
        legacy: row.priority
        for row in requirements
        for legacy in LEGACY_BY_REPLACEMENT.get(row.characteristic.code, ())
    }
    return [
        row
        for row in requirements
        if row.characteristic.code not in replacements
        or (
            row.priority == "required"
            and replacements[row.characteristic.code] != "required"
        )
    ]


def assess_requirements(p, requirements, observations):
    by_characteristic = defaultdict(list)
    for observation in observations:
        by_characteristic[observation.characteristic_id].append(observation)
    rows = [
        check_requirement(r, p, observations=by_characteristic[r.characteristic_id])
        for r in requirements
    ]
    mandatory = [r for r in rows if r["priority"] == "required"]
    status = (
        "blocker"
        if any(r["status"] == "blocker" for r in mandatory)
        else (
            "clarify"
            if not mandatory or any(r["status"] == "clarify" for r in mandatory)
            else "pass"
        )
    )
    return status, rows


def suitability_rating(rows, budget_met):
    if not any(row["priority"] == "required" for row in rows):
        return None, []
    characteristics = defaultdict(list)
    for row in rows:
        characteristics[row["characteristic_id"]].append(row)

    possible = earned = 0
    improvements = []
    for group in characteristics.values():
        required = [row for row in group if row["priority"] == "required"]
        weight = 3 if required else 1
        missing = [row for row in (required or group) if row["status"] != "pass"]
        possible += weight
        if missing:
            improvements.append(
                {
                    "weight": weight,
                    "requirements": [row["requirement"] for row in missing],
                    "code": missing[0]["code"],
                    "needs_changes": any(row["status"] != "clarify" for row in missing),
                }
            )
        else:
            earned += weight
    if budget_met is not None:
        possible += 2
        if budget_met:
            earned += 2
        else:
            improvements.append({"weight": 2, "budget": True})

    def scale(points):
        return (100 * points + possible // 2) // possible

    score = scale(earned) if any(row["status"] != "clarify" for row in rows) else None
    for item in improvements:
        item["gain"] = (
            scale(earned + item["weight"]) - score if score is not None else None
        )
    improvements.sort(key=lambda item: -item["weight"])
    return score, improvements


def ranking_observations(requirements, *, extra_codes=()):
    return (
        Observation.objects.filter(
            Q(characteristic_id__in={r.characteristic_id for r in requirements})
            | Q(characteristic__code__in=extra_codes)
        )
        .select_related("characteristic")
        .order_by("id")
    )


def assess_premises(req, p, requirements, observations, *, context):
    status, rows = assess_requirements(p, requirements, observations)
    costs = economics(p, req, context=context)
    reasons = defaultdict(list)
    for row in rows:
        reasons[row["status"]].append(row)
    budget_met = budget_result(p, costs)
    score, improvements = suitability_rating(rows, budget_met)
    budget_action = ""
    if req.budget is not None and budget_met is not True:
        if costs["complete"]:
            budget_action = f"Снизьте расходы на {money_text(costs['gap'])} в месяц."
        else:
            budget_action = "Уточните полную месячную нагрузку."
            if budget_met is False:
                budget_action += " Известные расходы уже превышают лимит."
            else:
                improvements.append({"budget": True, "gain": None})
        budget_action += f" Цель — до {money_text(req.budget)} в месяц."
    return {
        "p": p,
        "status": status,
        "score": score,
        "outcome": assess_outcome(req, p, status, rows, costs, context),
        "improvements": improvements,
        "budget_action": budget_action,
        "economics": costs,
        "rows": rows,
        "blockers": reasons["blocker"],
        "unclear": reasons["clarify"],
        "unclear_count": len({row["characteristic_id"] for row in reasons["clarify"]}),
        "matched": reasons["pass"],
        "unmet_preferences": reasons["desired_fail"],
        "evidence": evidence_summary(rows),
    }


def score_order(card):
    return -(card["score"] if card["score"] is not None else -1)


def rank_premises(req, *, requirements=None):
    if requirements is None:
        requirements = active_requirements(req)
    ready = any(r.priority == "required" for r in requirements)
    links = (
        req.premises_links.filter(premises__agency_id=req.agency_id)
        .select_related("premises")
        .prefetch_related(
            Prefetch(
                "premises__observations",
                queryset=ranking_observations(requirements),
                to_attr="ranking_observations",
            )
        )
    )
    cards = [
        assess_premises(
            req,
            link.premises,
            requirements,
            link.premises.ranking_observations,
            context=link,
        )
        for link in links
    ]
    cards.sort(
        key=lambda card: (
            outcome_priority(card),
            budget_excess(card),
            score_order(card),
            card["p"].pk,
        )
    )
    return cards, ready


@transaction.atomic
def evaluate(p, user, request_obj):
    from django.core.exceptions import PermissionDenied

    req = ClientRequest.objects.select_for_update().get(pk=request_obj.pk)
    p = Premises.objects.select_for_update().get(pk=p.pk)
    if req.owner_id != user.pk or req.agency_id != p.agency_id:
        raise PermissionDenied("Проверка недоступна.")
    context = RequestPremises.objects.filter(request=req, premises=p).first()
    if not context:
        raise ValueError("Помещение не добавлено в этот запрос.")
    requirements = list(active_requirements(req))
    if not any(r.priority == "required" for r in requirements):
        raise ValueError("Задайте хотя бы одно обязательное требование в профиле.")
    observations = p.observations.filter(
        characteristic_id__in={r.characteristic_id for r in requirements}
    ).order_by("id")
    status, rows = assess_requirements(p, requirements, observations)
    costs = economics(p, req, context=context)
    result = {
        "status": status,
        "label": LABELS[status],
        "rows": rows,
        "economics": costs,
        "outcome": assess_outcome(req, p, status, rows, costs, context),
        "financial": serial(financial_metrics(req, p, context, costs)),
        "fit_score": suitability_rating(rows, budget_result(p, costs))[0],
        "evidence_summary": evidence_summary(rows),
        "regulatory": regulatory_checklist(req, context),
    }
    latest = p.evaluations.filter(request=req).order_by("-id").first()
    if (
        latest
        and latest.current
        and latest.rules_version == RULES_VERSION
        and latest.result == result
    ):
        return latest
    ev = Evaluation.objects.create(
        premises=p,
        request=req,
        link_identity=context.pk,
        context_version=context.version,
        profile_version=req.version,
        premises_version=p.version,
        result=result,
        author=user,
        rules_version=RULES_VERSION,
    )
    audit(user, "check.completed", ev.pk)
    return ev


def current_evaluation(p, request_obj):
    ev = p.evaluations.filter(request=request_obj).order_by("-id").first()
    return ev if ev and ev.current and ev.rules_version == RULES_VERSION else None


def report_parameter_choices(req):
    return [
        (f"requirement:{r.pk}", str(r))
        for r in req.requirements.filter(priority="desired").order_by("id")
    ] + [
        ("economics:monthly", "Расшифровка ежемесячных платежей"),
        ("economics:once", "Расшифровка разовых платежей"),
        ("economics:stress", "Стрессовый сценарий"),
        ("future:leads", "Будущий модуль: карта лидов"),
    ]


def report_snapshot(
    req, objects, comment, parameters=None, *, report_type=CLIENT_SHORT, conclusion=""
):
    if report_type not in REPORT_FORMATS:
        raise ValueError("Выберите формат отчёта.")
    if conclusion and conclusion not in BROKER_CONCLUSIONS:
        raise ValueError("Выберите заключение риелтора.")
    choices = dict(report_parameter_choices(req))
    selected_parameters = set(choices) if parameters is None else set(parameters) & choices.keys()
    requirements = list(active_requirements(req))
    mandatory_ids = {r.pk for r in requirements if r.priority == "required"}
    wanted_ids = mandatory_ids | {
        r.pk for r in requirements if f"requirement:{r.pk}" in selected_parameters
    }
    items = []
    client_items = []
    identifiers = set()
    labels = Counter(p.label.strip().casefold() for p in objects)
    links = {
        link.premises_id: link
        for link in req.premises_links.filter(premises__in=objects)
    }
    for p in objects:
        identifier = " / ".join(
            filter(None, [p.label.strip(), p.floor.strip(), p.number.strip()])
        )
        if identifier.casefold() in identifiers:
            raise ValueError(
                "Выбранные варианты должны иметь разные однозначные метки. Уточните этаж / номер."
            )
        identifiers.add(identifier.casefold())
        ev = current_evaluation(p, req)
        if not ev:
            raise ValueError(
                f"{p.label}: оценка устарела или отсутствует. Пересчитайте перед выпуском PDF."
            )
        decision = ev.result["outcome"]["label"]
        items.append(
            {
                "id": p.pk,
                "title": identifier,
                "listing_url": p.listing_url,
                "evaluation_id": ev.pk,
                "checked_at": timezone.localtime(ev.created_at).isoformat(),
                "decision": decision,
                **ev.result,
                "rows": [
                    row
                    for row in ev.result["rows"]
                    if row["requirement_id"] in wanted_ids
                ],
            }
        )
        client_items.append(
            client_object(
                {
                    **items[-1],
                    "title": (
                        p.label.strip()
                        if labels[p.label.strip().casefold()] == 1
                        else identifier
                    ),
                    "rows": ev.result["rows"],
                    "commercial_conflict": p.commercial_status == "conflict",
                },
                district=p.district,
                area=p.area,
                opening=opening_costs(p, links.get(p.pk)),
                working_days=req.financial_plan.get("working_days"),
                pickup=req.business_format == "pickup",
            )
        )
    summary = client_summary(client_items)
    if conclusion == "recommend" and not summary["recommended"]:
        raise ValueError(
            "Заключение «Рекомендуем рассмотреть» доступно, когда хотя бы одно помещение "
            "подходит по проверенным условиям."
        )
    if not conclusion:
        conclusion = (
            "recommend" if summary["recommended"] else
            "conditional" if summary["conditional"] else "reject"
        )
    sections = CLIENT_SECTIONS if report_type == CLIENT_SHORT else [
        "Итоги сравнения и обязательные требования",
        "Техническая проверка и нормативные вопросы",
        "Финансовая модель и источники подтверждения",
        "Заключение риелтора и версия расчёта",
        *(label for key, label in choices.items() if key in selected_parameters),
    ]
    return serial(
        {
            "report_type": report_type,
            "format_label": REPORT_FORMATS[report_type],
            "layout_version": (
                "client-short-v1" if report_type == CLIENT_SHORT else "broker-detailed-v1"
            ),
            "sections": sections,
            "parameters": sorted(selected_parameters),
            "rules_version": RULES_VERSION,
            "request": req.title,
            "city": req.city,
            "business": req.business_format_label,
            "budget": req.budget,
            "start_budget": req.start_budget,
            "geography": district_description(req),
            "profile_version": req.version,
            "requirements": [
                f"{r.get_priority_display()}: {r}" for r in requirements
            ],
            "objects": items,
            "comment": comment,
            "broker_conclusion": {
                "code": conclusion,
                "label": BROKER_CONCLUSIONS[conclusion],
            },
            "client_content": {
                "objects": client_items,
                "summary": summary,
                "comment": public_text(comment),
            },
        }
    )


@transaction.atomic
def issue_report(
    req, objects, comment, user, key, parameters=None, *, report_type=CLIENT_SHORT,
    conclusion="",
):
    req = ClientRequest.objects.select_for_update().get(pk=req.pk)
    from django.core.exceptions import PermissionDenied

    if req.owner_id != user.pk:
        raise PermissionDenied
    if req.archived or req.stage == "done":
        raise ValueError("Верните запрос в работу перед выпуском отчёта.")
    old = Report.objects.filter(creation_key=key, request=req).first()
    if old:
        if old.snapshot.get("report_type", BROKER_DETAILED) != report_type:
            raise ValueError("Формат изменился после выпуска. Обновите форму отчёта.")
        return old
    # All mutations lock the same parent request, preventing mixed-version snapshots.
    requested_ids = {p.pk for p in objects}
    objects = list(
        Premises.objects.select_for_update()
        .filter(requests=req, agency_id=req.agency_id, pk__in=requested_ids)
        .order_by("id")
    )
    if len(objects) != len(requested_ids):
        raise ValueError("Не все помещения добавлены в этот запрос.")
    if not 1 <= len(objects) <= 4:
        raise ValueError("Выберите от 1 до 4 вариантов.")
    snap = report_snapshot(
        req, objects, comment, parameters=parameters, report_type=report_type,
        conclusion=conclusion,
    )
    identity = snap
    if report_type == CLIENT_SHORT:
        # Broker-only parameter choices do not change the short document.
        identity = {
            **snap,
            "parameters": [],
            "objects": [
                (obj["id"], obj["evaluation_id"]) for obj in snap["objects"]
            ],
        }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
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


def compare_rows(objects, differences=False, request_obj=None):
    if request_obj is None:
        raise ValueError("Укажите клиентский запрос.")
    # A missing evaluation is never presented as a current recommendation.
    evaluations = [current_evaluation(p, request_obj) for p in objects]
    rows = []
    for i, req in enumerate(active_requirements(request_obj)) if objects else []:
        cells = []
        critical = False
        for ev in evaluations:
            cell = (
                next(
                    (r for r in ev.result["rows"] if r.get("requirement_id") == req.pk),
                    None,
                )
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
            rows.append({"name": str(req), "cells": cells, "requirement_id": req.pk})
    return rows
