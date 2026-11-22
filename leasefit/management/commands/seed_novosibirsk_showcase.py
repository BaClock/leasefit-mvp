"""Import the fixed Novosibirsk fixture into an explicitly marked demo database."""

import json
import os
from collections import Counter
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from leasefit.business import FORMATS, QUESTIONS
from leasefit.metric_forms import FinancialPlanForm
from leasefit.models import (
    Agency,
    Characteristic,
    ClientRequest,
    Membership,
    Observation,
    Premises,
    Requirement,
)
from leasefit.operations import STRUCTURED, normalize_value

DATASET = "leasefit-novosibirsk-showcase-2026-v1"
DATA_PATH = settings.BASE_DIR / "tests" / "data" / "novosibirsk_demo_20x12.json"
AGENCY = "DEMO LeaseFIT · Новосибирск · учебные данные"
USERNAME = "novosibirsk-showcase@example.invalid"
CITY = "Новосибирск"
CONFIRMATION = (
    "СИНТЕТИЧЕСКОЕ учебное подтверждение. "
    "Не является реальным техническим документом."
)
ORIGIN = "demo-client"
COMMERCIAL = {
    "rent_rub_month": "rent",
    "operating_rub_month": "operating",
    "utilities_rub_month": "utilities",
    "other_monthly_rub": "other_monthly",
    "deposit_rub": "deposit",
    "commission_rub": "commission",
    "vat_mode": "vat_mode",
    "source": "commercial_source",
    "source_type": "commercial_source_type",
    "status": "commercial_status",
}
PLAN = {
    "revenue_rub_month": "revenue",
    "average_rub": "average",
    "variable_rub_per_operation": "variable",
    "fixed_rub_month": "fixed",
    "working_days": "working_days",
    "target_profit_rub_month": "target_profit",
    "custom_drop_pct": "custom_drop",
}


def stable_key(external_id):
    return uuid5(
        NAMESPACE_URL,
        f"https://github.com/BaClock/leasefit-mvp/{DATASET}/{external_id}",
    )


def require_demo_database(disposable):
    if not (
        disposable
        and settings.DEBUG
        and os.getenv("LEASEFIT_SHOWCASE") == "1"
        and connection.vendor == "postgresql"
        and connection.settings_dict["NAME"] == "leasefit_showcase"
    ):
        raise CommandError(
            "Нужны DEBUG=1, LEASEFIT_SHOWCASE=1, --disposable-db и отдельная "
            "база PostgreSQL leasefit_showcase."
        )


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def remap(fields, mapping):
    require(
        isinstance(fields, dict) and fields.keys() == mapping.keys(),
        "Неверный набор полей финансовых данных.",
    )
    return {target: fields[source] for source, target in mapping.items()}


def load_data():
    try:
        data = json.loads(
            DATA_PATH.read_text(encoding="utf-8-sig"), parse_float=Decimal
        )
        meta = data["metadata"]
        require(
            meta["dataset_id"] == DATASET
            and meta["dataset_version"] == 1
            and meta["city"] == CITY
            and meta["agency_name"] == AGENCY
            and meta["fictional"] is True
            and all(
                meta[key] is False
                for key in ("real_ads", "real_documents", "real_contacts")
            ),
            "Нужны метаданные вымышленного набора LeaseFIT для Новосибирска.",
        )
        for field, prefix, count in (
            ("premises", "P", 20),
            ("business_profiles", "B", 12),
        ):
            rows = data[field]
            require(
                isinstance(rows, list) and len(rows) == count,
                f"{field}: ожидалось {count} записей.",
            )
            require(
                {row["external_id"] for row in rows}
                == {f"NSK-{prefix}{number:02d}" for number in range(1, count + 1)},
                f"{field}: отсутствуют или повторяются внешние идентификаторы.",
            )
        require(
            len({row["district"] for row in data["premises"]}) == 10,
            "Ожидаются 10 учебных районов.",
        )
        cases = data["acceptance_cases"]
        require(
            isinstance(cases, list) and len(cases) == 13,
            "Ожидаются 13 контрольных пар.",
        )
        profile_ids = {row["external_id"] for row in data["business_profiles"]}
        premises_ids = {row["external_id"] for row in data["premises"]}
        require(
            all(
                case["profile"] in profile_ids
                and case["premises"] in premises_ids
                and case["expect_technical"] in ("pass", "clarify", "blocker")
                and isinstance(case["reason"], str)
                and case["reason"].strip()
                for case in cases
            )
            and len({(case["profile"], case["premises"]) for case in cases})
            == len(cases),
            "Контрольные пары должны иметь существующие ID и допустимый статус.",
        )
        return data
    except (OSError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise CommandError(f"Ошибка формата {DATA_PATH.name}: {exc}") from exc


def value_fields(characteristic, value, *, requirement=False):
    kind = characteristic.value_type
    if value is None and not requirement:
        return {}
    if kind in STRUCTURED:
        return {"structured_value": normalize_value(kind, characteristic.code, value)}
    if kind == "boolean":
        require(type(value) is bool, "Ожидается логическое значение.")
        return {"expected" if requirement else "boolean": value}
    if kind == "number":
        require(type(value) in (int, Decimal), "Ожидается число.")
        return {"minimum" if requirement else "value": value}
    require(isinstance(value, str) and value.strip(), "Ожидается непустая строка.")
    return {"expected_text" if requirement else "text": value}


def prepared_rows(data, chars):
    premises, profiles = [], []
    for row in data["premises"]:
        external_id = row["external_id"]
        try:
            require(
                row["city"] == CITY and row["label"].startswith(f"ДЕМО {external_id}"),
                "Нужны учебная метка с ID и город Новосибирск.",
            )
            require(
                type(row["location_confirmed"]) is bool and row["district"].strip(),
                "Проверьте район и подтверждение города.",
            )
            values = {
                "label": row["label"],
                "city": CITY,
                "district": row["district"],
                "location_confirmed": row["location_confirmed"],
                "floor": row["floor"],
                "number": row["number"],
                "premises_type": row["premises_type"],
                "area": row["area_sqm"],
                **remap(row["commercial"], COMMERCIAL),
            }
            require(
                values["commercial_status"] == "unverified"
                and values["commercial_source_type"] == "manual",
                "Ожидаются учебные коммерческие условия без подтверждения.",
            )
            specs = row["specs"]
            unknown, unverified = set(row["unknown_specs"]), set(
                row["unverified_specs"]
            )
            require(
                unknown == {key for key, value in specs.items() if value is None}
                and unverified <= specs.keys() - unknown,
                "Списки неизвестных и неподтверждённых значений противоречат specs.",
            )
            require(
                specs["area_sqm"] == row["area_sqm"],
                "Два значения площади различаются.",
            )
            observations = []
            for code, value in specs.items():
                char = chars[code]
                status = (
                    "unknown"
                    if code in unknown
                    else "unverified" if code in unverified else "confirmed"
                )
                observations.append(
                    (
                        code,
                        {
                            "characteristic": char,
                            "name": char.label,
                            "unit": char.unit,
                            "status": status,
                            "source_type": "manual",
                            "source": (
                                CONFIRMATION
                                if status == "confirmed"
                                else row["source_note"]
                            ),
                            "confirmation": (
                                CONFIRMATION if status == "confirmed" else ""
                            ),
                            "value": None,
                            "boolean": None,
                            "text": "",
                            "structured_value": None,
                            **value_fields(char, value),
                        },
                    )
                )
            premises.append((external_id, values, observations))
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise CommandError(f"{external_id}: {exc}") from exc

    for row in data["business_profiles"]:
        external_id = row["external_id"]
        try:
            answers = row["business_answers"]
            require(
                row["city"] == CITY
                and row["title"].startswith("ДЕМО ")
                and row["no_initial_premises_links"] is True
                and row["business_format"] in FORMATS,
                "Неверный учебный профиль.",
            )
            require(
                isinstance(answers, dict)
                and answers.keys() <= QUESTIONS.keys()
                and all(answer in ("yes", "no") for answer in answers.values()),
                "Недопустимый ответ бизнес-анкеты.",
            )
            values = {
                "title": row["title"],
                "city": CITY,
                "business": row["business"],
                "business_format": row["business_format"],
                "business_answers": answers,
                "budget": row["monthly_budget_rub"],
                "start_budget": row["start_budget_rub"],
            }
            form = FinancialPlanForm(
                {"version": 1, **remap(row["financial_plan"], PLAN)},
                request_obj=ClientRequest(**values),
            )
            require(form.is_valid(), f"Финансовый план: {form.errors.as_text()}")
            values["financial_plan"] = form.plan()
            requirements = []
            for rule in row["requirements"]:
                char = chars[rule["code"]]
                operator = rule["operator"]
                require(
                    rule["is_normative"] is False
                    and rule["origin"] == ORIGIN
                    and rule["priority"] in ("required", "desired")
                    and operator in char.allowed_operators
                    and operator != "range",
                    f"Недопустимое условие {char.code}.",
                )
                requirements.append(
                    {
                        "characteristic": char,
                        "name": char.label,
                        "unit": char.unit,
                        "operator": operator,
                        "origin": ORIGIN,
                        "priority": rule["priority"],
                        **value_fields(char, rule["value"], requirement=True),
                    }
                )
            require(
                len({rule["name"] for rule in requirements}) == len(requirements)
                and any(rule["priority"] == "required" for rule in requirements),
                "Нужны уникальные условия, включая хотя бы одно обязательное.",
            )
            profiles.append((external_id, values, requirements))
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise CommandError(f"{external_id}: {exc}") from exc
    return premises, profiles


def demo_owner():
    user = User.objects.filter(username=USERNAME).first()
    agencies = list(Agency.objects.filter(name=AGENCY).order_by("pk"))
    if user is None and not agencies:
        agency = Agency.objects.create(name=AGENCY)
        user = User.objects.create_user(USERNAME)
        Membership.objects.create(user=user, agency=agency, role="manager")
        return agency, user
    require(
        user is not None
        and not user.is_staff
        and not user.is_superuser
        and len(agencies) == 1
        and Membership.objects.filter(
            user=user, agency=agencies[0], role="manager", active=True
        ).exists(),
        "Учебное имя пользователя или агентства уже занято.",
    )
    return agencies[0], user


def save_row(model, lookup, values, counts, *, scope=None, defaults=None):
    row = model.objects.select_for_update().filter(**lookup).first()
    created = row is None
    scope = scope or {}
    if created:
        row = model(**lookup, **scope, **(defaults or {}))
    else:
        require(
            all(getattr(row, field) == value for field, value in scope.items()),
            f"Учебный идентификатор занят: {model.__name__}, {lookup}.",
        )
    changed = [
        field
        for field, value in values.items()
        if created or getattr(row, field) != value
    ]
    if created or changed:
        for field, value in values.items():
            setattr(row, field, value)
        row.full_clean()
        row.save() if created else row.save(update_fields=changed)
        counts[model.__name__, "created" if created else "updated"] += 1
    return row, created, bool(changed)


def update_parent_version(parent):
    parent.version += 1
    parent.save(update_fields=["version", "updated_at"])


@transaction.atomic
def import_data(data):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)", [stable_key(DATASET).int >> 65]
            )
    chars = Characteristic.objects.filter(agency__isnull=True).in_bulk(
        field_name="code"
    )
    premises, profiles = prepared_rows(data, chars)
    agency, user = demo_owner()
    counts = Counter()

    for external_id, values, observations in premises:
        obj, created, changed = save_row(
            Premises,
            {"creation_key": stable_key(external_id)},
            values,
            counts,
            scope={"agency": agency},
        )
        for code, fields in observations:
            _, _, observation_changed = save_row(
                Observation,
                {"creation_key": stable_key(f"{external_id}/{code}")},
                fields,
                counts,
                scope={"premises": obj, "author": user},
                defaults={"observed_on": timezone.localdate()},
            )
            changed |= observation_changed
        if changed and not created:
            update_parent_version(obj)

    for external_id, values, requirements in profiles:
        req, created, changed = save_row(
            ClientRequest,
            {"creation_key": stable_key(external_id)},
            values,
            counts,
            scope={"agency": agency, "owner": user},
        )
        for fields in requirements:
            name = fields.pop("name")
            _, _, requirement_changed = save_row(
                Requirement,
                {"request": req, "name": name},
                fields,
                counts,
                scope={"origin": fields.pop("origin")},
            )
            changed |= requirement_changed
        if changed and not created:
            update_parent_version(req)
    return counts


class Command(BaseCommand):
    help = "Загрузить 20 учебных помещений и 12 профилей Новосибирска."

    def add_arguments(self, parser):
        parser.add_argument("--disposable-db", action="store_true")

    def handle(self, *args, **options):
        require_demo_database(options["disposable_db"])
        try:
            counts = import_data(load_data())
        except ValidationError as exc:
            raise CommandError(f"Импорт отменён: {exc}") from exc
        for model in (Premises, ClientRequest, Observation, Requirement):
            name = model.__name__
            self.stdout.write(
                f"{name}: создано {counts[name, 'created']}, обновлено {counts[name, 'updated']}"
            )
        self.stdout.write(f"Учебная учётная запись: {USERNAME}")
