"""Create the fictional Novosibirsk acceptance fixture in a disposable database."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from leasefit.models import (
    Agency,
    Characteristic,
    ClientRequest,
    Membership,
    Observation,
    Premises,
    RequestPremises,
    Requirement,
)


FIXTURE_AGENCY = "Тест LeaseFIT · Новосибирск · вымышленные помещения"
FIXTURE_USER = "novosibirsk-fixture@example.invalid"


@transaction.atomic
def seed(*, include_links=True):
    user = User.objects.filter(username=FIXTURE_USER).first()
    if user:
        membership = Membership.objects.filter(
            user=user, agency__name=FIXTURE_AGENCY
        ).first()
        if not membership:
            raise CommandError(
                "Имя тестового пользователя уже занято другой учётной записью."
            )
        return user, list(
            ClientRequest.objects.filter(agency=membership.agency).order_by("id")
        )
    agency = Agency.objects.create(name=FIXTURE_AGENCY)
    user = User.objects.create_user(username=FIXTURE_USER)
    Membership.objects.create(user=user, agency=agency)
    codes = {
        char.code: char for char in Characteristic.objects.filter(agency__isnull=True)
    }
    premises = []
    for label, district, power, rent in [
        ("Помещение A · Ленинский район", "Ленинский", 15, 60000),
        ("Помещение B · Центральный район", "Центральный", 30, 73000),
        ("Помещение C · Октябрьский район", "Октябрьский", 25, 65000),
    ]:
        obj = Premises.objects.create(
            agency=agency,
            label=label,
            city="Новосибирск",
            district=district,
            location_confirmed=True,
            area=60,
            rent=rent,
            operating=8000,
            utilities=5000,
            other_monthly=0,
            vat_mode="included",
            deposit=rent,
            commission=0,
            commercial_source="Вымышленные учебные условия",
        )
        premises.append(obj)

        def observe(code, status="confirmed", **value):
            char = codes[code]
            Observation.objects.create(
                premises=obj,
                characteristic=char,
                name=char.label,
                unit=char.unit,
                author=user,
                source_type="document",
                source="ВЫМЫШЛЕННЫЙ учебный источник",
                status=status,
                observed_on=timezone.localdate(),
                confirmation=(
                    "Тестовое значение для учебного сценария"
                    if status == "confirmed"
                    else ""
                ),
                **value,
            )

        observe("area_sqm", value=Decimal(60))
        observe("power_kw", value=Decimal(power))
        observe("water_supply", boolean=True)
        observe("separate_entrance", boolean=True)
        if power == 15:
            observe("separate_exhaust", boolean=False)
            observe("visitor_entry", structured_value="open")
            observe(
                "operating_schedule",
                structured_value={
                    "mode": "weekly",
                    "intervals": [[day, 540, 1320] for day in range(7)],
                },
            )
        elif power == 30:
            observe("ventilation", status="unverified", boolean=True)
        else:
            observe("operating_schedule", status="unknown")

    requests = []
    for format_code, name, power in [
        ("food.coffee_to_go", "Кофейня навынос", 15),
        ("food.cafe_kitchen", "Кафе с кухней", 25),
        ("pickup.standard", "ПВЗ", 10),
    ]:
        request = ClientRequest.objects.create(
            agency=agency,
            owner=user,
            title=f"{name} · учебный запрос",
            city="Новосибирск",
            business=name,
            business_format=format_code,
            budget=85000,
            start_budget=240000,
            business_answers=(
                {"preparation": "yes", "food_storage": "yes", "food_delivery": "yes"}
                if format_code.startswith("food.")
                else {}
            ),
            financial_plan={
                "revenue": "500000",
                "average": "100" if format_code.startswith("pickup.") else "500",
                "variable": "30" if format_code.startswith("pickup.") else "200",
                "fixed": "130000",
                "working_days": 25,
                "custom_drop": "30",
            },
        )
        requests.append(request)
        Requirement.objects.create(
            request=request,
            characteristic=codes["power_kw"],
            name="Учебный порог мощности",
            unit="кВт",
            minimum=power,
        )
        if format_code == "food.cafe_kitchen":
            Requirement.objects.create(
                request=request,
                characteristic=codes["separate_exhaust"],
                name="Отдельная вытяжка для выбранной кухни",
                operator="eq",
            )
        if format_code == "pickup.standard":
            Requirement.objects.create(
                request=request,
                characteristic=codes["unloading_methods"],
                name="Нужен служебный вход для разгрузки",
                operator="eq",
                structured_value=["service"],
            )
        for obj in premises if include_links else ():
            RequestPremises.objects.create(
                request=request,
                premises=obj,
                adaptation=40000,
                equipment=60000,
                initial_stock=20000,
                other_once=0,
                cash_reserve=50000,
            )
    return user, requests


class Command(BaseCommand):
    help = "Создаёт вымышленный набор Новосибирска в отдельной учебной базе."

    def add_arguments(self, parser):
        parser.add_argument(
            "--disposable-db",
            action="store_true",
            help="Подтверждение использования отдельной учебной базы.",
        )
        parser.add_argument(
            "--unlinked",
            action="store_true",
            help="В новой учебной базе оставить запросы без выбранных помещений.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG or not options["disposable_db"]:
            raise CommandError(
                "Требуется отдельная учебная база, DEBUG=1 и --disposable-db."
            )
        _, requests = seed(include_links=not options["unlinked"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Учебный набор готов. Запросы: {', '.join(str(req.pk) for req in requests)}."
            )
        )
