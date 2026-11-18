"""Versioned business formats and recommendations without legal thresholds."""

from dataclasses import dataclass

TEMPLATE_VERSION = "1"


@dataclass(frozen=True)
class BusinessFormat:
    label: str
    defaults: tuple[str, ...] = ()


CATEGORIES = {
    "food": (
        "Общественное питание",
        {
            "coffee_to_go": BusinessFormat(
                "Кофейня навынос", ("water_supply", "sewerage", "ventilation")
            ),
            "coffee_seating": BusinessFormat(
                "Кофейня с посадочными местами",
                ("water_supply", "sewerage", "ventilation", "accessible_entrance"),
            ),
            "cafe_limited": BusinessFormat(
                "Кафе без полного цикла приготовления",
                ("water_supply", "sewerage", "ventilation"),
            ),
            "cafe_kitchen": BusinessFormat(
                "Кафе с собственной кухней",
                ("water_supply", "sewerage", "ventilation", "separate_exhaust"),
            ),
            "bakery": BusinessFormat(
                "Пекарня", ("water_supply", "sewerage", "ventilation")
            ),
        },
    ),
    "pickup": (
        "Пункты выдачи заказов",
        {
            "standard": BusinessFormat("Стандартный ПВЗ", ("accessible_entrance",)),
            "storage": BusinessFormat(
                "ПВЗ с большой зоной хранения", ("storage_zone",)
            ),
            "oversize": BusinessFormat(
                "ПВЗ с приёмкой крупногабаритных товаров",
                ("storage_zone", "truck_access"),
            ),
        },
    ),
    "retail": (
        "Розничная торговля",
        {
            "grocery": BusinessFormat(
                "Продуктовый магазин", ("storage_zone", "ventilation")
            ),
            "nonfood": BusinessFormat(
                "Непродовольственный магазин", ("display_windows",)
            ),
            "specialized": BusinessFormat(
                "Специализированный магазин", ("storage_zone",)
            ),
        },
    ),
    "services": (
        "Услуги",
        {
            "beauty": BusinessFormat(
                "Салон красоты", ("water_supply", "sewerage", "ventilation")
            ),
            "hair": BusinessFormat("Парикмахерская", ("water_supply", "sewerage")),
            "repair": BusinessFormat("Ремонтная мастерская", ("ventilation",)),
        },
    ),
    "office": (
        "Офисы",
        {
            "clients": BusinessFormat(
                "Офис с приёмом клиентов", ("accessible_entrance", "heating")
            ),
            "internal": BusinessFormat("Офис без приёма клиентов", ("heating",)),
        },
    ),
    "other": (
        "Другое",
        {"individual": BusinessFormat("Индивидуальный профиль бизнеса")},
    ),
}

FORMATS = {
    f"{category}.{key}": spec
    for category, (_, formats) in CATEGORIES.items()
    for key, spec in formats.items()
}
FORMAT_CHOICES = [
    (label, [(f"{category}.{key}", spec.label) for key, spec in formats.items()])
    for category, (label, formats) in CATEGORIES.items()
]

QUESTIONS = {
    "preparation": "Будет ли приготовление пищи или напитков?",
    "hot_kitchen": "Будет ли горячая кухня?",
    "seating": "Предусмотрены посадочные места?",
    "baking": "Будет ли собственная выпечка?",
    "food_delivery": "Нужна ли приёмка продуктов?",
    "food_storage": "Будут ли храниться продукты или сырьё?",
    "extraction": "Нужна ли отдельная вытяжка по технологии вашего производства?",
    "storage": "Нужна ли зона хранения?",
    "customers": "Будет ли приём клиентов?",
    "oversize": "Планируется ли приём крупногабаритных товаров?",
    "unloading": "Нужна ли отдельная зона разгрузки?",
}
QUESTION_GROUPS = {
    "food": (
        "preparation",
        "hot_kitchen",
        "seating",
        "baking",
        "food_delivery",
        "food_storage",
        "extraction",
    ),
    "pickup": ("storage", "customers", "oversize", "unloading"),
}


def recommendations(format_code, answers):
    proposed = {code: True for code in FORMATS[format_code].defaults}
    for question, additions in {
        "preparation": ("water_supply", "sewerage"),
        "hot_kitchen": ("ventilation",),
        "seating": ("accessible_entrance",),
        "baking": ("ventilation",),
        "food_delivery": ("storage_zone",),
        "food_storage": ("storage_zone",),
        "extraction": ("separate_exhaust",),
        "storage": ("storage_zone",),
        "customers": ("accessible_entrance",),
        "oversize": ("storage_zone", "truck_access"),
    }.items():
        if answers.get(question) == "yes":
            proposed.update(dict.fromkeys(additions, True))
    for code in ("visitor_entry", "operating_schedule", "unloading_methods"):
        if answers.get(code):
            proposed[code] = answers[code]
    if format_code.startswith("pickup.") and answers.get("unloading") == "yes":
        proposed.setdefault("unloading_methods", ["dock"])
    if answers.get("extraction") == "no":
        proposed.pop("separate_exhaust", None)
    return proposed
