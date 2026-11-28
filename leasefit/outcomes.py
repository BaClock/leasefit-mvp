"""Presentation conclusions from the existing technical and cost calculations."""

from decimal import Decimal, ROUND_HALF_UP

from .metrics import opening_costs

GROUPS = (
    "recommended",
    "finance_clarify",
    "over_budget",
    "technical_clarify",
    "blocker",
    "location",
)
GROUP_ORDER = {group: position for position, group in enumerate(GROUPS)}
TECHNICAL_LABELS = {
    "pass": "Технически подходит",
    "clarify": "Технические требования требуют уточнения",
    "blocker": "Не соответствует техническим требованиям",
}


def money_text(value):
    value = Decimal(str(value))
    places = 0 if value == value.to_integral() else 2
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",") + " ₽"


def monthly_outcome(p, req, costs):
    gap = Decimal(costs["gap"]) if costs["gap"] is not None else None
    conflict = costs["rent_conflict"] or p.commercial_status == "conflict"
    confirmed = p.commercial_status == "confirmed" and not conflict
    amount = (
        costs["monthly_total"]
        if any(value is not None for _, value in costs["monthly"][:-1])
        else None
    )
    state = (
        "unset"
        if gap is None
        else (
            "clarify"
            if conflict
            else "over" if gap > 0 else "within" if costs["complete"] else "clarify"
        )
    )
    labels = {
        "unset": "Бюджет не задан",
        "clarify": "Требуется уточнить расходы",
        "within": "В пределах бюджета",
        "over": f"Превышен бюджет на {money_text(gap)}" if state == "over" else "",
    }
    if state == "over" and not confirmed:
        labels["over"] = (
            f"По указанным расходам превышение составляет {money_text(gap)}"
        )
    elif state == "over" and not costs["complete"]:
        labels["over"] = f"Известные расходы выше лимита на {money_text(gap)}"
    percent = (
        str(
            (gap / req.budget * 100).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
        ).replace(".", ",")
        if state == "over" and req.budget > 0
        else None
    )
    return {
        "state": state,
        "label": labels[state],
        "budget": str(req.budget) if req.budget is not None else None,
        "amount": amount,
        "complete": costs["complete"],
        "gap": str(gap) if state in ("over", "within") else None,
        "difference": str(abs(gap)) if state in ("over", "within") else None,
        "sign": (
            "+" if state == "over" else "−" if state == "within" and gap < 0 else ""
        ),
        "percent": percent,
        "evidence": (
            "Коммерческие данные противоречивы. Сверьте аренду, ставку и остальные платежи."
            if conflict
            else (
                "Коммерческие условия подтверждены."
                if confirmed
                else "Коммерческие условия требуют подтверждения."
            )
        ),
        "confirmed": confirmed,
        "source": costs["commercial_evidence"]["source"],
    }


def opening_limit(p, req, context, costs):
    values = opening_costs(p, context)
    if (
        not values
        or req.start_budget is None
        or any(value is None for _, value in values)
        or costs["rent_conflict"]
        or p.commercial_status == "conflict"
    ):
        return None
    total = sum(value for _, value in values)
    gap = total - req.start_budget
    if gap <= 0:
        return None
    return {
        "kind": "start",
        "title": (
            f"Стартовые расходы выше бюджета на {money_text(gap)}"
            if p.commercial_status == "confirmed"
            else f"По указанным затратам стартовый бюджет превышен на {money_text(gap)}"
        ),
        "budget": str(req.start_budget),
        "amount": str(total),
        "difference": str(gap),
        "action": "Пересмотрите затраты на открытие или стартовый бюджет.",
    }


def assess_outcome(req, p, status, rows, costs, context, *, preliminary=False):
    monthly = monthly_outcome(p, req, costs)
    over = monthly["state"] == "over"
    finance_clear = monthly["state"] == "within" and monthly["confirmed"]
    start = opening_limit(p, req, context, costs)
    if preliminary:
        # An unknown expense cannot prove compliance. Known costs can exceed a limit.
        finance_clear = monthly["state"] in {"within", "unset"} and costs["complete"]
        if req.start_budget is not None:
            startup = opening_costs(p, context)
            start_known = startup and all(value is not None for _, value in startup)
            if not start_known or costs["rental_deposit_unknown"]:
                finance_clear = False
                known = sum((value or Decimal(0) for _, value in startup), Decimal(0)) if startup else (
                    (p.deposit or Decimal(0)) + (p.commission or Decimal(0))
                )
                if known > req.start_budget and not costs["rent_conflict"] and p.commercial_status != "conflict":
                    gap = known - req.start_budget
                    start = {
                        "kind": "start", "title": f"Известные стартовые затраты выше бюджета на {money_text(gap)}",
                        "difference": str(gap), "action": "Уточните стартовые затраты.",
                    }
    finance_issues = []
    if over:
        finance_issues.append(
            {
                "kind": "monthly",
                "title": (
                    "Месячная нагрузка выше бюджета"
                    if monthly["complete"]
                    else "Известные расходы выше бюджета"
                ),
                "action": (
                    "Рассмотрите более доступный вариант или пересмотрите финансовую модель."
                    if req.budget > 0 and Decimal(monthly["gap"]) >= req.budget
                    else "Согласуйте снижение расходов или пересмотрите допустимый бюджет."
                ),
            }
        )
    if start:
        finance_issues.append(start)
    blockers = [
        {
            "kind": "technical",
            "title": row["requirement"],
            "detail": row["value"],
            "action": row["adjustment"],
        }
        for row in rows
        if row["status"] == "blocker"
    ]
    questions = [
        {"kind": "question", "title": row["requirement"], "detail": row["reason"]}
        for row in rows
        if row["status"] == "clarify" and row["priority"] == "required"
    ]
    if not finance_clear and not over:
        questions.append(
            {
                "kind": "question",
                "title": (
                    "Подтвердите коммерческие условия"
                    if monthly["state"] == "within"
                    else monthly["label"]
                ),
                "detail": ", ".join(costs["missing"]),
            }
        )
    if status == "blocker":
        group, label = "blocker", "Есть критические технические несоответствия"
    elif status != "pass":
        group = "technical_clarify"
        label = "Требует технического уточнения"
        if not finance_clear and not over:
            label = "Требует технического и финансового уточнения"
    elif over or start:
        group = "over_budget"
        label = (
            "Не подходит по текущему бюджету"
            if over
            else "Не подходит по стартовому бюджету"
        )
    elif finance_clear:
        group, label = "recommended", "Подходит по имеющимся данным" if preliminary else "Подходит по проверенным условиям"
    else:
        group, label = "finance_clarify", "Требует финансового уточнения"
    return {
        "group": group,
        "label": label,
        "technical_label": TECHNICAL_LABELS[status],
        "monthly": monthly,
        "financial_limits": finance_issues,
        "primary": (blockers[:1] + finance_issues + blockers[1:] + questions)[:2],
    }


def outcome_priority(card):
    group = card.get("catalog_group", card["outcome"]["group"])
    return GROUP_ORDER[group], GROUP_ORDER[card["outcome"]["group"]]


def budget_excess(card):
    """Use absolute monthly excess, then one-off excess, within budget results."""
    outcome = card["outcome"]
    if outcome["group"] != "over_budget":
        return ()
    start = next(
        (
            item["difference"]
            for item in outcome["financial_limits"]
            if item["kind"] == "start"
        ),
        "0",
    )
    return (
        (
            Decimal(outcome["monthly"]["gap"] or 0)
            if outcome["monthly"]["state"] == "over"
            else Decimal(0)
        ),
        Decimal(start),
    )
