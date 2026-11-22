from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from .business import FORMATS
from .management.commands.seed_novosibirsk import seed
from .metric_forms import FinancialPlanForm
from .models import Agency, Characteristic, Membership, Observation, Requirement
from .operation_forms import WeeklyScheduleField
from .operations import GENERAL_ACCESS, matches
from .regulations import regulatory_checklist
from .services import (
    active_requirements,
    check_requirement,
    current_evaluation,
    evaluate,
    issue_report,
    rank_premises,
)
from .tests import grouped_payload


class ExpandedMVPTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, (cls.coffee, cls.cafe, cls.pickup) = seed()
        cls.a = cls.coffee.premises.order_by("pk").first()

    def setUp(self):
        self.client.force_login(self.user)

    def test_profile_yes_no_for_access_and_unloading_with_one_schedule(self):
        url = reverse("profile", args=[self.coffee.pk])
        page = self.client.get(url)
        self.assertContains(page, "Нужен ли посетителям вход в помещение?")
        self.assertContains(page, "Планируется ли разгрузка товаров?")
        self.assertContains(page, "График работы бизнеса", count=1)
        self.assertNotContains(page, "Работа в необходимое время")
        payload = grouped_payload(
            {
                "title": self.coffee.title,
                "city": self.coffee.city,
                "business": self.coffee.business,
                "version": self.coffee.version,
                "creation_key": self.coffee.creation_key,
                "visitor_entry_priority": "required",
                "unloading_methods_priority": "required",
            },
            self.coffee,
        )
        self.assertRedirects(
            self.client.post(url, payload), reverse("refine", args=[self.coffee.pk])
        )
        access = self.coffee.requirements.get(characteristic__code="visitor_entry")
        unloading = self.coffee.requirements.get(
            characteristic__code="unloading_methods"
        )
        self.assertEqual(access.structured_value, GENERAL_ACCESS["visitor_entry"])
        self.assertEqual(
            unloading.structured_value, GENERAL_ACCESS["unloading_methods"]
        )
        self.assertEqual(check_requirement(access, self.a)["status"], "pass")
        self.assertEqual(check_requirement(unloading, self.a)["status"], "clarify")
        Observation.objects.create(
            premises=self.a,
            characteristic=unloading.characteristic,
            name=unloading.name,
            structured_value=["none"],
            author=self.user,
            observed_on=date(2026, 11, 19),
            status="confirmed",
            confirmation="Учебные данные",
            source="Учебные данные",
        )
        self.assertEqual(check_requirement(unloading, self.a)["status"], "blocker")
        self.coffee.refresh_from_db()
        payload.update(
            version=self.coffee.version,
            visitor_entry_priority="na",
            unloading_methods_priority="na",
        )
        self.assertRedirects(
            self.client.post(url, payload), reverse("refine", args=[self.coffee.pk])
        )
        self.assertFalse(
            any(
                row.pk in {access.pk, unloading.pk}
                for row in active_requirements(self.coffee)
            )
        )

    def test_template_yes_uses_general_requirements_and_no_ignores_old_details(self):
        url = reverse("business_setup", args=[self.coffee.pk])
        payload = {"action": "preview", "business_format": self.coffee.business_format}
        result = self.client.post(
            url, {**payload, "customers": "yes", "unloading": "yes"}
        )
        values = {value for _, value in result.context["choices"]}
        self.assertIn("Любой доступ для посетителей", values)
        self.assertIn("Любой доступный способ разгрузки", values)
        result = self.client.post(
            url,
            {
                **payload,
                "customers": "no",
                "unloading": "no",
                "visitor_entry": "open",
                "unloading_methods": "dock",
            },
        )
        self.assertEqual(
            [
                label
                for label, _ in result.context["choices"]
                if label
                in (
                    "Как посетители должны попадать в помещение?",
                    "Какой способ разгрузки необходим?",
                )
            ],
            [],
        )

    def test_template_preview_apply_and_repeat_preserve_manual_values(self):
        url = reverse("business_setup", args=[self.pickup.pk])
        self.assertEqual(len(FORMATS), 17)
        response = self.client.post(
            url,
            {
                "action": "preview",
                "business_format": "pickup.oversize",
                "unloading": "yes",
                "visitor_entry": "open",
                "operating_schedule_0": "weekly",
                "operating_schedule_1": "21:00",
                "operating_schedule_2": "02:00",
            },
        )
        self.assertContains(response, "Посмотреть рекомендации")
        self.assertContains(response, "Подтверждаю смену категории")
        self.assertTrue(response.context["preview"])
        payload = {"action": "apply", "preview": response.context["preview"]}
        self.assertRedirects(self.client.post(url, payload), url)
        self.assertEqual(self.pickup.requirements.count(), 2)
        payload["confirm_category"] = "yes"
        self.assertRedirects(
            self.client.post(url, payload), reverse("profile", args=[self.pickup.pk])
        )
        self.pickup.refresh_from_db()
        self.assertEqual(self.pickup.business_format, "pickup.oversize")
        schedule = self.pickup.requirements.get(
            characteristic__code="operating_schedule"
        )
        self.assertEqual(schedule.structured_value["intervals"], [[0, 1260, 120]])
        self.assertEqual(schedule.origin, "pickup.oversize@1")
        self.assertEqual(
            self.pickup.requirements.get(characteristic__code="power_kw").minimum, 10
        )
        self.assertEqual(
            self.pickup.requirements.get(
                characteristic__code="unloading_methods"
            ).structured_value,
            ["service"],
        )
        count = self.pickup.requirements.count()
        version = self.pickup.version
        repeat = self.client.post(
            url,
            {
                "action": "preview",
                "business_format": "pickup.oversize",
                **{
                    "unloading": "yes",
                    "visitor_entry": "open",
                    "operating_schedule_0": "weekly",
                    "operating_schedule_1": "21:00",
                    "operating_schedule_2": "02:00",
                },
            },
        )
        self.client.post(url, {"action": "apply", "preview": repeat.context["preview"]})
        self.pickup.refresh_from_db()
        self.assertEqual(self.pickup.version, version)
        self.assertEqual(self.pickup.requirements.count(), count)

    def test_schedule_checks_overnight_and_week_boundary(self):
        weekly = {"mode": "weekly", "intervals": [[6, 1320, 120]]}
        self.assertTrue(
            matches(
                "time_schedule", {"mode": "weekly", "intervals": [[0, 30, 90]]}, weekly
            )
        )
        self.assertFalse(
            matches(
                "time_schedule", {"mode": "weekly", "intervals": [[0, 30, 180]]}, weekly
            )
        )
        self.assertFalse(matches("time_schedule", {"mode": "always"}, weekly))
        self.assertTrue(matches("time_schedule", weekly, {"mode": "always"}))
        field = WeeklyScheduleField()
        self.assertEqual(
            field.clean(["weekly", "09:00", "24:00", "22:00", "01:00"])["intervals"],
            [[0, 540, 1440], [1, 1320, 60]],
        )
        char = Characteristic.objects.get(code="operating_schedule")
        req = Requirement.objects.create(
            request=self.coffee,
            characteristic=char,
            name=char.label,
            operator="eq",
            structured_value={"mode": "always"},
        )
        self.assertEqual(check_requirement(req, self.a)["status"], "blocker")
        obj_c = self.coffee.premises.order_by("pk").last()
        legacy = Characteristic.objects.get(code="working_hours")
        Observation.objects.create(
            premises=obj_c,
            characteristic=legacy,
            name=legacy.label,
            boolean=True,
            author=self.user,
            observed_on=date(2026, 11, 19),
            source_type="owner",
            source="Учебный исторический ответ",
            status="confirmed",
            confirmation="Без указания часов",
        )
        self.assertEqual(check_requirement(req, obj_c)["status"], "clarify")

    def test_visitor_entry_and_unloading_use_structured_values(self):
        entry = Characteristic.objects.get(code="visitor_entry")
        req = Requirement.objects.create(
            request=self.pickup,
            characteristic=entry,
            name=entry.label,
            operator="eq",
            structured_value="open",
        )
        self.assertEqual(check_requirement(req, self.a)["status"], "pass")
        req.structured_value = "controlled"
        self.assertEqual(check_requirement(req, self.a)["status"], "blocker")
        unloading = self.pickup.requirements.get(
            characteristic__code="unloading_methods"
        )
        Observation.objects.create(
            premises=self.a,
            characteristic=unloading.characteristic,
            name=unloading.name,
            structured_value=["yard", "service"],
            author=self.user,
            observed_on=date(2026, 11, 19),
            source_type="owner",
            source="Учебный регламент",
            status="confirmed",
            confirmation="Вымышленные способы разгрузки",
        )
        self.assertEqual(check_requirement(unloading, self.a)["status"], "pass")
        unloading.structured_value = ["dock"]
        self.assertEqual(check_requirement(unloading, self.a)["status"], "blocker")

    def test_same_object_businesses_and_financial_metrics(self):
        first = evaluate(self.a, self.user, self.coffee)
        second = evaluate(self.a, self.user, self.cafe)
        third = evaluate(self.a, self.user, self.pickup)
        self.assertEqual(
            [sample.result["status"] for sample in (first, second, third)],
            ["pass", "blocker", "clarify"],
        )
        finance = first.result["financial"]
        self.assertEqual(finance["profit_rent"], "157000.00")
        self.assertEqual(finance["occupancy_share"], "14.60")
        self.assertEqual(
            (finance["break_even"], finance["daily_break_even"]), (677, 28)
        )
        self.assertEqual(finance["opening"], "230000.00")
        self.assertEqual(
            [case["profit"] for case in finance["scenarios"]],
            ["97000.00", "67000.00", "37000.00", "7000.00"],
        )
        self.assertEqual(finance["max_drop"], "32.33")
        self.assertEqual(
            first.result["evidence_summary"],
            {"confirmed": 1, "unconfirmed": 0, "unknown": 0},
        )
        self.assertEqual(rank_premises(self.coffee)[0][0]["score"], 100)
        response = self.client.get(reverse("compare", args=[self.coffee.pk]))
        self.assertContains(response, "Методики всех 18 показателей")
        self.assertContains(response, "677 операций в месяц")
        report = issue_report(self.coffee, [self.a], "", self.user, uuid4())
        raw = bytes(report.pdf)
        parsed = "\n".join(
            page.extract_text() for page in PdfReader(BytesIO(raw)).pages
        )
        self.assertIn("Безубыточность", parsed)
        self.assertIn("СанПиН", parsed)
        self.coffee.financial_plan["variable"] = "600"
        self.coffee.version += 1
        self.coffee.save()
        self.assertIsNone(current_evaluation(self.a, self.coffee))
        current = evaluate(self.a, self.user, self.coffee)
        self.assertIsNone(current.result["financial"]["break_even"])
        report.refresh_from_db()
        self.assertEqual(bytes(report.pdf), raw)

    def test_regulatory_applicability_and_versioned_review(self):
        self.assertFalse(regulatory_checklist(self.pickup))
        self.assertTrue(
            all(
                item["code"].startswith(("tr-", "fire-"))
                for item in regulatory_checklist(self.coffee, on_date=date(2032, 9, 1))
            )
        )
        link = self.coffee.premises_links.get(premises=self.a)
        items = regulatory_checklist(self.coffee, link, on_date=date(2026, 11, 19))
        self.assertEqual(
            next(item for item in items if item["code"] == "san-water")["status"],
            "pending",
        )
        route = reverse("regulatory_review", args=[self.coffee.pk, self.a.pk])
        data = {
            "rule": "san-water",
            "status": "positive",
            "note": "Вымышленный акт",
            "version": link.version,
            "profile_version": self.coffee.version,
            "premises_version": self.a.version,
        }
        self.assertEqual(self.client.post(route, data).status_code, 302)
        link.refresh_from_db()
        self.assertEqual(link.regulatory_reviews["san-water"]["status"], "positive")
        link.premises.version += 1
        self.assertEqual(
            next(
                item
                for item in regulatory_checklist(self.coffee, link)
                if item["code"] == "san-water"
            )["status"],
            "pending",
        )
        self.coffee.business_answers["preparation"] = "no"
        self.assertEqual(
            next(
                item
                for item in regulatory_checklist(self.coffee, link)
                if item["code"] == "san-flow"
            )["status"],
            "na",
        )
        other = type(self.user).objects.create_user("outsider-fixture@example.invalid")
        Membership.objects.create(
            user=other, agency=Agency.objects.create(name="Другое агентство")
        )
        self.client.force_login(other)
        self.assertEqual(self.client.post(route, data).status_code, 404)

    def test_financial_headers_and_missing_values(self):
        form = FinancialPlanForm({"version": 1}, request_obj=self.pickup)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("Вознаграждение", form.fields["average"].label)
        self.assertEqual(form.plan(), {})
        self.pickup.financial_plan = {}
        self.pickup.save()
        self.a.utilities = None
        self.a.save()
        finance = evaluate(self.a, self.user, self.pickup).result["financial"]
        self.assertIsNone(finance["profit_rent"])
        self.assertIsNone(finance["occupancy_share"])
        self.assertIsNone(finance["break_even"])
        self.assertEqual(finance["scenarios"], [])
