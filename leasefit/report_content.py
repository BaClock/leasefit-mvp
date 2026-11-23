"""Client-facing presentation of the results already saved by LeaseFIT."""

import re
from decimal import Decimal

from .outcomes import money_text

CLIENT_SHORT = "client_short"
BROKER_DETAILED = "broker_detailed"
REPORT_FORMATS = {
    CLIENT_SHORT: "Краткий отчёт для клиента",
    BROKER_DETAILED: "Подробный отчёт для риелтора",
}
BROKER_CONCLUSIONS = {
    "recommend": "Рекомендуем рассмотреть",
    "conditional": "Можно рассмотреть после уточнений",
    "reject": "Не рекомендуем при текущих условиях",
}
CLIENT_SECTIONS = (
    "Результат подбора",
    "Почему помещения подходят или не подходят",
    "Финансовая оценка",
    "Следующие шаги перед арендой",
)
PRIVATE_LINK = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.|/(?:media|uploads?|attachments?|private)/|"
    r"[a-z]:[\\/]|\\\\)[^\s<>]+",
    re.IGNORECASE,
)


def public_text(value):
    return PRIVATE_LINK.sub("[ссылка скрыта]", value)


def client_object(obj, *, district, area, opening, working_days, pickup):
    outcome = obj["outcome"]
    monthly = outcome["monthly"]
    costs = obj["economics"]
    rows = obj["rows"]
    blockers = [
        {
            "title": f"Обязательное условие не выполнено: {row['requirement']}",
            "action": row["adjustment"]
            or f"Уточните, можно ли выполнить условие «{row['requirement']}».",
        }
        for row in rows
        if row["status"] == "blocker"
    ]
    financial = [
        {
            "title": monthly["label"] if limit["kind"] == "monthly" else limit["title"],
            "action": limit["action"],
        }
        for limit in outcome["financial_limits"]
    ]
    questions = [
        {
            "title": f"Требует уточнения: {row['requirement']}",
            "action": f"Подтвердите у собственника: {row['requirement']}.",
        }
        for row in rows
        if row["status"] == "clarify" and row["priority"] == "required"
    ]
    if costs["missing"]:
        questions.append(
            {
                "title": "Полная сумма расходов требует уточнения",
                "action": "Сверьте с собственником: "
                + ", ".join(costs["missing"]).lower() + ".",
            }
        )
    elif not monthly["confirmed"]:
        questions.append(
            {
                "title": monthly["evidence"],
                "action": "Запросите у собственника подтверждение ежемесячных платежей.",
            }
        )
    if monthly["budget"] is None:
        questions.append(
            {
                "title": "Лимит расходов в месяц не задан",
                "action": "Определите допустимые расходы на помещение в месяц.",
            }
        )
    rules = [
        {
            "title": f"Нужно проверить: {rule['title']}",
            "action": f"Проверьте применимость требования «{rule['title']}».",
        }
        for rule in obj.get("regulatory", [])
        if rule["status"] == "pending"
    ]
    all_issues = blockers + financial + questions + rules
    head = [items[0] for items in (blockers, financial, questions) if items]
    limits = (head + [issue for issue in all_issues if issue not in head])[:3]
    actions = [item["action"] for item in all_issues]
    f = obj["financial"]
    if any(value is not None for _, value in opening) and f["opening_missing"]:
        actions.append(
            "Уточните расходы на запуск: "
            + ", ".join(f["opening_missing"]).lower() + "."
        )
    blocked = bool(blockers or financial)
    if blockers:
        verdict = "Не подходит по обязательным техническим условиям"
        comparison_status = "Критическое ограничение"
    elif financial and obj["status"] == "pass":
        verdict = "Технически подходит, но " + financial[0]["title"][0].lower() + financial[0]["title"][1:]
        comparison_status = "Выше бюджета"
    elif financial:
        verdict = obj["decision"] + ". " + financial[0]["title"]
        comparison_status = "Выше бюджета, нужны уточнения"
    else:
        verdict = obj["decision"]
        comparison_status = "Подходит" if outcome["group"] == "recommended" else "Нужно уточнить"
    if monthly["difference"] is not None:
        difference = f"{monthly['sign']}{money_text(monthly['difference'])}"
        if not monthly["complete"]:
            difference += " по известным платежам"
    else:
        difference = monthly["label"]
    known_opening = any(value is not None for _, value in opening)
    opening_comparison = None
    if known_opening and not f["opening_missing"] and f["opening_gap"] is not None:
        gap = Decimal(f["opening_gap"])
        if costs["rent_conflict"] or obj["commercial_conflict"]:
            opening_comparison = "Сравнение с бюджетом требует сверки данных"
        elif gap > 0:
            opening_comparison = financial[-1]["title"]
        else:
            opening_comparison = "По указанным суммам в пределах стартового бюджета"
    return {
        "title": public_text(obj["title"]),
        "district": public_text(district) if district else "Район не указан",
        "area": f"{area:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " м²"
        if area is not None else "Площадь не указана",
        "monthly": monthly,
        "monthly_label": "Расходы в месяц" if monthly["complete"] else "Известные расходы в месяц",
        "difference": difference,
        "technical": outcome["technical_label"],
        "verdict": verdict,
        "comparison_status": comparison_status,
        "limits": limits,
        "benefits": [
            row["requirement"] for row in sorted(rows, key=lambda row: row["priority"] != "required")
            if row["status"] == "pass"
        ][:3],
        "actions": list(dict.fromkeys(actions)),
        "blocked": blocked,
        "recommended": outcome["group"] == "recommended" and not blocked,
        "has_blocker": bool(blockers),
        "has_finance_limit": bool(financial),
        "has_questions": bool(questions),
        "monthly_rows": costs["monthly"],
        "monthly_missing": costs["missing"],
        "opening_rows": opening,
        "opening_total": f["opening"] if known_opening else None,
        "opening_missing": f["opening_missing"],
        "opening_comparison": opening_comparison,
        "break_even": f["break_even"],
        "daily_break_even": f["daily_break_even"],
        "working_days": working_days,
        "pickup": pickup,
        "scenarios": f["scenarios"][:3],
    }


def client_summary(objects):
    numbered = list(enumerate(objects, 1))
    recommended = [str(i) for i, obj in numbered if obj["recommended"]]
    conditional = [str(i) for i, obj in numbered if not obj["recommended"] and not obj["blocked"]]
    excluded = [str(i) for i, obj in numbered if obj["blocked"]]
    headlines = []
    if recommended:
        headlines.append("Есть подходящие варианты")
    if any(obj["has_blocker"] for obj in objects):
        headlines.append("Есть критические технические ограничения")
    if any(obj["has_finance_limit"] for obj in objects):
        headlines.append("Помещения превышают бюджет")
    if conditional:
        headlines.append("Есть варианты, но требуется уточнение")
    elif any(obj["has_questions"] for obj in objects):
        headlines.append("Часть данных требует уточнения")
    actions = {}
    for i, obj in numbered:
        for action in obj["actions"]:
            actions.setdefault(action, []).append(str(i))
    return {
        "headlines": headlines,
        "recommendation": (
            "В первую очередь рассмотрите варианты, которые подходят по проверенным условиям."
            if recommended else
            "На основании текущих данных нельзя рекомендовать ни один из вариантов без уточнения или изменения условий."
        ),
        "recommended": recommended,
        "conditional": conditional,
        "excluded": excluded,
        "actions": [{"text": action, "objects": numbers} for action, numbers in actions.items()],
    }
