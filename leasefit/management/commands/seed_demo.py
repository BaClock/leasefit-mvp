import json, secrets
from decimal import Decimal
from datetime import timedelta, date
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from leasefit.models import *
from leasefit.services import evaluate


class Command(BaseCommand):
    help = "Вымышленный учебный сценарий. Не запускайте на рабочей базе."

    def add_arguments(self, p):
        p.add_argument("--allow-non-debug", action="store_true")
        p.add_argument(
            "--fresh-invitation",
            action="store_true",
            help="Новое вымышленное приглашение для браузерного теста",
        )

    @transaction.atomic
    def handle(self, *args, **o):
        if not settings.DEBUG and not o["allow_non_debug"]:
            raise CommandError(
                "Учебные данные доступны при DEBUG=1. Для изолированной учебной базы: --allow-non-debug."
            )
        agency, _ = Agency.objects.get_or_create(name="Демо · Сибирь Пространство")
        for email, name, role in [
            ("leader@example.invalid", "Мария · руководитель", "manager"),
            ("agent@example.invalid", "Алексей · риелтор", "realtor"),
        ]:
            user, created = User.objects.get_or_create(
                username=email, defaults={"email": email, "first_name": name}
            )
            if created:
                password = secrets.token_urlsafe(16)
                user.set_password(password)
                user.save()
                self.stdout.write(f"{email} — пароль: {password}")
            Membership.objects.get_or_create(
                user=user, defaults={"agency": agency, "role": role}
            )
        owner = User.objects.get(username="agent@example.invalid")
        leader = User.objects.get(username="leader@example.invalid")
        req, created = ClientRequest.objects.get_or_create(
            owner=owner,
            title="Кофейня на левом берегу · учебный запрос",
            defaults={
                "agency": agency,
                "budget": 70000,
                "start_budget": 180000,
                "stage": "active",
            },
        )
        if created:
            Requirement.objects.create(
                request=req, name="Мощность", unit="кВт", operator="gte", minimum=15
            )
            Requirement.objects.create(
                request=req,
                name="Отдельная вытяжка",
                unit="",
                operator="eq",
                expected=True,
            )
            for label, power, vent, total, kind in [
                ("Вариант А · учебный проспект, 1", 15, True, 73000, "document"),
                ("Вариант Б · учебная улица, 2", 15, None, 65000, "listing"),
                ("Вариант В · учебная площадь, 3", 12, True, 59000, "document"),
            ]:
                p = Premises.objects.create(
                    request=req,
                    label=label,
                    commercial_source_type="owner",
                    commercial_source="Вымышленное письмо с условиями аренды",
                    commercial_date=timezone.localdate(),
                    commercial_status="confirmed",
                    commercial_confirmation="Учебное подтверждение всех сумм и площади в письме",
                    area=60,
                    rent=total - 13000,
                    rate=1000 if total == 73000 else None,
                    operating=8000,
                    utilities=5000,
                    other_monthly=0,
                    vat_mode="included",
                    deposit=60000,
                    deposit_refundable=True,
                    commission=20000,
                    adaptation=90000,
                    other_once=0,
                    stress=5000,
                    assumptions="Условные суммы. Реального предложения аренды нет.",
                    internal_notes="ВНУТРЕННЯЯ ЗАМЕТКА — только для команды",
                    listing_url="https://example.invalid/demo",
                )
                Observation.objects.create(
                    premises=p,
                    name="Мощность",
                    value=power,
                    unit="кВт",
                    source_type=kind,
                    source="Условный акт подключения"
                    if kind == "document"
                    else "Условное объявление",
                    observed_on=timezone.localdate(),
                    status="confirmed" if kind == "document" else "unverified",
                    confirmation="Учебное подтверждение по вымышленному акту"
                    if kind == "document"
                    else "",
                    author=owner,
                )
                if vent is not None:
                    Observation.objects.create(
                        premises=p,
                        name="Отдельная вытяжка",
                        boolean=vent,
                        source_type="owner",
                        source="Учебный письменный ответ собственника",
                        observed_on=timezone.localdate(),
                        status="confirmed",
                        confirmation="Условный ответ по помещению",
                        author=owner,
                    )
                if total == 65000:
                    # Archive examples are isolated from current premises facts.
                    samples = json.loads(
                        (
                            settings.BASE_DIR / "tests/data/archive_samples.json"
                        ).read_text()
                    )
                    for item in samples:
                        Observation.objects.create(
                            premises=p,
                            name=item["name"],
                            value=item.get("value"),
                            text=item.get("text", ""),
                            unit=item.get("unit", ""),
                            scope=item["scope"],
                            locator=item.get("locator", ""),
                            source_type="archive",
                            source=item["source"],
                            source_url=item["source_url"],
                            observed_on=date.fromisoformat(item["date"]),
                            status="unverified",
                            author=owner,
                        )
                evaluate(p, owner)
        email = "new-agent@example.invalid"
        if o["fresh_invitation"] or User.objects.filter(username=email).exists():
            email = "new-agent-" + secrets.token_hex(6) + "@example.invalid"
        inv = Invitation.objects.filter(
            agency=agency, email=email, accepted_at=None, expires_at__gt=timezone.now()
        ).first()
        if not inv:
            inv = Invitation.objects.create(
                agency=agency,
                email=email,
                created_by=leader,
                expires_at=timezone.now() + timedelta(days=7),
            )
        self.stdout.write(f"Приглашение: /invite/{inv.token}")
        self.stdout.write(
            "Учебный сценарий готов. Повторный запуск не изменяет существующие карточки и пароли."
        )
