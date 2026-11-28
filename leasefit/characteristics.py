"""Stable standard definitions. Values are client choices, never business norms."""

import hashlib
import re

GROUPS = [
    ("layout", "Площадь и планировка"),
    ("engineering", "Инженерные коммуникации"),
    ("access", "Условия доступа и работы помещения"),
    ("specialized", "Складские и производственные характеристики"),
]


def definition(code, label, group, kind="boolean", unit="", primary=False, aliases=()):
    return dict(
        code=code,
        label=label,
        group=group,
        value_type=kind,
        unit=unit,
        primary=primary,
        aliases=list(aliases),
        allowed_operators=["gte", "lte", "range", "eq"] if kind == "number" else ["eq"],
    )


STANDARD = [
    definition(
        "area_sqm",
        "Площадь",
        "layout",
        "number",
        "м²",
        True,
        ["Общая площадь", "Площадь помещения", "Архивная площадь", "area"],
    ),
    definition("floor", "Этаж", "layout", "text", "", True, ["Этаж помещения"]),
    definition("separate_entrance", "Отдельный вход", "layout", primary=True),
    definition(
        "ceiling_height_m",
        "Высота потолков",
        "layout",
        "number",
        "м",
        aliases=["Высота", "Высота помещения"],
    ),
    definition(
        "display_windows", "Витринные окна", "layout", aliases=["Наличие витрин"]
    ),
    definition("redevelopment", "Возможность перепланировки", "layout"),
    definition(
        "room_count", "Количество отдельных помещений", "layout", "number", "шт."
    ),
    definition(
        "accessible_entrance", "Доступность для маломобильных посетителей", "layout"
    ),
    definition(
        "power_kw",
        "Электрическая мощность",
        "engineering",
        "number",
        "кВт",
        True,
        [
            "Мощность",
            "Выделенная мощность",
            "Электромощность",
            "Электричество",
            "power",
        ],
    ),
    definition(
        "water_supply",
        "Водоснабжение",
        "engineering",
        primary=True,
        aliases=["Вода", "Подключение воды"],
    ),
    definition("sewerage", "Канализация", "engineering", primary=True),
    definition("ventilation", "Вентиляция", "engineering", primary=True),
    definition(
        "three_phase",
        "Трёхфазное электроснабжение",
        "engineering",
        aliases=["Трехфазное электроснабжение"],
    ),
    definition("power_upgrade", "Возможность увеличения мощности", "engineering"),
    definition(
        "separate_exhaust",
        "Отдельная вытяжка",
        "engineering",
        aliases=["Кухонная вытяжка"],
    ),
    definition("supply_ventilation", "Приточная вентиляция", "engineering"),
    definition("heating", "Отопление", "engineering"),
    definition("air_conditioning", "Кондиционирование", "engineering"),
    definition("wet_points", "Дополнительные мокрые точки", "engineering"),
    definition("visitor_access", "Доступ посетителей", "access", primary=True),
    definition(
        "working_hours",
        "Работа в необходимое время",
        "access",
        primary=True,
        aliases=["Возможность работы в необходимое время"],
    ),
    definition(
        "loading_access",
        "Возможность разгрузки товара",
        "access",
        primary=True,
        aliases=["Разгрузка", "Разгрузка товара"],
    ),
    definition("parking", "Парковка", "access"),
    definition("truck_access", "Доступ грузового транспорта", "access"),
    definition("access_24h", "Режим работы 24/7", "access", aliases=["Доступ 24/7"]),
    definition(
        "extra_exit",
        "Дополнительный эвакуационный выход",
        "access",
        aliases=["Второй выход"],
    ),
    definition(
        "use_restrictions", "Ограничения по использованию помещения", "access", "text"
    ),
    definition(
        "visitor_entry",
        "Как посетители должны попадать в помещение?",
        "access",
        "enum",
        primary=True,
    ),
    definition(
        "operating_schedule",
        "В какое время бизнес должен иметь возможность работать?",
        "access",
        "time_schedule",
        primary=True,
    ),
    definition(
        "unloading_methods",
        "Какой способ разгрузки необходим?",
        "access",
        "multi_select",
        primary=True,
    ),
    definition(
        "delivery_vehicles", "Тип транспорта для поставок", "access", "multi_select"
    ),
    definition(
        "delivery_schedule", "Разрешённые часы поставок", "access", "time_schedule"
    ),
    definition("delivery_approach", "Подъезд непосредственно к входу", "access"),
    definition("noise_limits", "Ограничения по шуму", "access", "text"),
    definition("storage_zone", "Зона хранения", "layout"),
    definition("repair_state", "Состояние ремонта", "layout", "text"),
    definition("first_line", "Первая линия", "layout"),
    definition("hot_water", "Горячая вода", "engineering"),
    definition("restroom", "Санузел", "engineering"),
    definition("gas_supply", "Газоснабжение", "engineering"),
    definition("service_entrance", "Служебный вход", "access"),
    definition("signage", "Размещение вывески", "access"),
    definition("listing_access_hours", "Часы доступа по объявлению", "access", "text"),
    definition("floor_load", "Нагрузка на пол", "specialized", "number", "т/м²"),
    definition("gate_height", "Высота ворот", "specialized", "number", "м"),
    definition("gate_width", "Ширина ворот", "specialized", "number", "м"),
    definition("loading_docks", "Доковые ворота", "specialized"),
    definition("semi_trailer_access", "Подъезд фуры", "specialized"),
    definition("temperature_mode", "Температурный режим", "specialized", "text"),
    definition("overhead_crane", "Крановое оборудование", "specialized"),
    definition("cold_storage", "Холодильное оборудование", "specialized"),
]
LEGACY_ACCESS = {"visitor_access", "working_hours", "loading_access", "access_24h"}
for spec in STANDARD:
    if spec["code"] in LEGACY_ACCESS:
        spec["primary"] = False
BY_CODE = {d["code"]: d for d in STANDARD}


def normalized(value):
    return re.sub(r"\s+", " ", value.strip().casefold().replace("ё", "е"))


ALIASES = {
    normalized(name): d["code"]
    for d in STANDARD
    for name in [d["label"], *d["aliases"]]
}


def custom_code(agency_id, name, unit, kind):
    digest = hashlib.sha256(f"{normalized(name)}|{unit}|{kind}".encode()).hexdigest()[
        :20
    ]
    return f"custom_{agency_id}_{digest}"


def resolve_characteristic(agency_id, name, unit="", kind="number"):
    from .models import Characteristic

    code = ALIASES.get(normalized(name))
    if code:
        spec = BY_CODE[code]
        return Characteristic.objects.get_or_create(
            code=code,
            defaults={
                k: v for k, v in spec.items() if k not in ["primary", "aliases", "code"]
            },
        )[0]
    return Characteristic.objects.get_or_create(
        code=custom_code(agency_id, name, unit, kind),
        defaults={
            "label": name,
            "group": "custom",
            "value_type": kind,
            "unit": unit,
            "agency_id": agency_id,
            "allowed_operators": (
                ["gte", "lte", "range", "eq"] if kind == "number" else ["eq"]
            ),
        },
    )[0]
