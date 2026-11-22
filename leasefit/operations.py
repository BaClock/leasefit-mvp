"""Canonical values for premises access and weekly local-time schedules."""

from django.core.exceptions import ValidationError

ENTRY = {
    "open": "Свободный вход без пропусков",
    "controlled": "По пропускам или согласованию",
    "staff": "Только для сотрудников",
}
UNLOADING = {
    "street": "Через основной вход с улицы",
    "yard": "Через вход со двора",
    "service": "Через отдельный служебный вход",
    "dock": "Через специализированную разгрузочную зону",
    "none": "Разгрузка невозможна",
}
VEHICLES = {
    "car": "Легковой автомобиль",
    "van": "Фургон",
    "truck": "Грузовой автомобиль",
}
ACCESS_CHOICES = {
    "visitor_entry": ENTRY,
    "unloading_methods": UNLOADING,
    "delivery_vehicles": VEHICLES,
}
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
DAY = 24 * 60
WEEK = 7 * DAY
STRUCTURED = {"enum", "multi_select", "time_schedule"}
CUSTOM_KINDS = {"number", "boolean", "text"}
GENERAL_ACCESS = {"visitor_entry": "any_visitor", "unloading_methods": ["any_method"]}
ACCESS_QUESTIONS = {
    "visitor_entry": "Нужен ли посетителям вход в помещение?",
    "unloading_methods": "Планируется ли разгрузка товаров?",
    "operating_schedule": "График работы бизнеса",
}
GENERAL_ACCESS_LABELS = {
    "visitor_entry": "Любой доступ для посетителей",
    "unloading_methods": "Любой доступный способ разгрузки",
}
LEGACY_BY_REPLACEMENT = {
    "visitor_entry": {"visitor_access"},
    "operating_schedule": {"working_hours", "access_24h"},
    "unloading_methods": {"loading_access"},
}


def normalize_value(kind, code, value, *, requirement=False):
    if requirement and code in GENERAL_ACCESS and value == GENERAL_ACCESS[code]:
        return value
    if (
        kind == "enum"
        and isinstance(value, str)
        and value in ACCESS_CHOICES.get(code, {})
    ):
        return value
    if (
        kind == "multi_select"
        and isinstance(value, list)
        and value
        and all(
            isinstance(item, str) and item in ACCESS_CHOICES.get(code, {})
            for item in value
        )
    ):
        options = sorted(set(value))
        if "none" not in options or (options == ["none"] and not requirement):
            return options
    if kind == "time_schedule" and isinstance(value, dict):
        if value.get("mode") == "always":
            return {"mode": "always"}
        intervals = value.get("intervals")
        if value.get("mode") == "weekly" and isinstance(intervals, list) and intervals:
            valid = all(
                isinstance(item, list)
                and len(item) == 3
                and all(type(part) is int for part in item)
                and 0 <= item[0] < 7
                and 0 <= item[1] < DAY
                and 0 <= item[2] <= DAY
                and item[1] != item[2]
                for item in intervals
            )
            if valid and len(intervals) <= 35:
                return {
                    "mode": "weekly",
                    "intervals": [
                        list(item) for item in sorted(set(map(tuple, intervals)))
                    ],
                }
    raise ValidationError("Укажите допустимое значение выбранной характеристики.")


def weekly_coverage(schedule):
    if schedule["mode"] == "always":
        return [(0, WEEK)]
    slices = []
    for day, start, end in schedule["intervals"]:
        start_at = day * DAY + start
        end_at = day * DAY + end + (DAY if end < start else 0)
        slices.append((start_at, min(end_at, WEEK)))
        if end_at > WEEK:
            slices.append((0, end_at - WEEK))
    merged = []
    for start, end in sorted(slices):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def matches(kind, expected, actual):
    if kind == "enum" and expected == GENERAL_ACCESS["visitor_entry"]:
        return actual in {"open", "controlled"}
    if kind == "multi_select" and expected == GENERAL_ACCESS["unloading_methods"]:
        return bool(actual) and actual != ["none"]
    if kind == "time_schedule":
        coverage = weekly_coverage(actual)
        return all(
            any(a <= start and end <= b for a, b in coverage)
            for start, end in weekly_coverage(expected)
        )
    if kind == "multi_select":
        return set(expected) <= set(actual)
    return expected == actual


def value_label(kind, code, value, *, requirement=False):
    if value is None:
        return ""
    try:
        value = normalize_value(kind, code, value, requirement=requirement)
    except ValidationError:
        return ""
    if requirement and code in GENERAL_ACCESS and value == GENERAL_ACCESS[code]:
        return GENERAL_ACCESS_LABELS[code]
    if kind == "time_schedule":
        if value["mode"] == "always":
            return "Круглосуточно"

        def clock(minutes):
            return f"{minutes // 60:02d}:{minutes % 60:02d}"

        return ", ".join(
            f"{DAYS[day]} {clock(start)}–{clock(end)}"
            for day, start, end in value["intervals"]
        )
    if kind == "multi_select":
        return ", ".join(ACCESS_CHOICES[code][item] for item in value)
    return ACCESS_CHOICES[code][value]
