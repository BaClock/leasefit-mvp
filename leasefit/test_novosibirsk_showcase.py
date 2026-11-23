from copy import deepcopy
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4
from io import BytesIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from .management.commands import seed_novosibirsk_showcase as showcase
from .metrics import financial_metrics
from .outcomes import assess_outcome, budget_excess, monthly_outcome, outcome_priority
from .models import (
    Agency,
    ClientRequest,
    Membership,
    Observation,
    Premises,
    Report,
    RequestPremises,
)
from .services import current_evaluation, economics, evaluate, issue_report, report_snapshot


class ShowcaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.other_user = User.objects.create_user("other-showcase-agency")
        cls.other_agency = Agency.objects.create(name="Прежнее агентство")
        Membership.objects.create(user=cls.other_user, agency=cls.other_agency)
        cls.historical = Premises.objects.create(
            agency=cls.other_agency, label="История"
        )
        cls.data = showcase.load_data()
        cls.counts = showcase.import_data(cls.data)
        cls.agency = Agency.objects.get(name=showcase.AGENCY)
        cls.user = User.objects.get(username=showcase.USERNAME)

    def setUp(self):
        self.client.force_login(self.user)

    def profile(self, external_id):
        return ClientRequest.objects.get(creation_key=showcase.stable_key(external_id))

    def premises(self, external_id):
        return Premises.objects.get(creation_key=showcase.stable_key(external_id))

    def results(self, req):
        response = self.client.get(reverse("objects", args=[req.pk]))
        self.assertEqual(response.status_code, 200)
        return list(response.context["recommendation_page"])

    def test_budget_excess_is_visible_and_prioritized_at_equal_fit_scores(self):
        req = self.profile("NSK-B01")
        req.budget = Decimal(95000)
        req.save(update_fields=["budget"])
        p01, p11 = self.premises("NSK-P01"), self.premises("NSK-P11")
        page = self.client.get(reverse("objects", args=[req.pk]))
        cards = [
            card
            for card in page.context["recommendation_groups"]["over_budget"]
            if card["p"] in (p01, p11)
        ]
        self.assertEqual([card["p"] for card in cards], [p01, p11])
        self.assertEqual(cards[0]["score"], cards[1]["score"])
        for card, difference, percent in zip(
            cards, ("28000.00", "173000.00"), ("29,5", "182,1")
        ):
            self.assertEqual(card["status"], "pass")
            self.assertEqual(
                card["outcome"]["label"], "Не подходит по текущему бюджету"
            )
            self.assertEqual(card["outcome"]["monthly"]["difference"], difference)
            self.assertEqual(card["outcome"]["monthly"]["percent"], percent)
        for phrase in (
            "28 000 ₽",
            "173 000 ₽",
            "Коммерческие условия требуют подтверждения",
        ):
            self.assertContains(page, phrase)

    def test_financial_status_handles_incomplete_conflicting_and_zero_budget(self):
        req = self.profile("NSK-B01")
        obj = self.premises("NSK-P01")
        req.budget = Decimal(95000)
        obj.utilities = None

        def monthly():
            return monthly_outcome(obj, req, economics(obj, req, context=None))

        self.assertEqual(monthly()["state"], "over")
        self.assertEqual(Decimal(monthly()["amount"]), 117000)
        self.assertFalse(monthly()["complete"])
        self.assertFalse(monthly()["confirmed"])
        req.budget = Decimal(117000)
        self.assertEqual(monthly()["state"], "clarify")
        self.assertIsNone(monthly()["difference"])
        req.budget = Decimal(0)
        self.assertEqual(monthly()["state"], "over")
        self.assertIsNone(monthly()["percent"])
        obj.rate = Decimal(1)
        self.assertEqual(monthly()["state"], "clarify")
        req.budget = None
        self.assertEqual(monthly()["state"], "unset")
        req.budget = Decimal(130000)
        obj.rate = None
        obj.utilities = Decimal(6000)
        obj.vat_mode, obj.vat_rate = "excluded", Decimal(20)
        self.assertEqual(Decimal(monthly()["amount"]), 145000)
        self.assertEqual(monthly()["state"], "over")
        obj.rent = Decimal(50000)
        obj.commercial_status = "confirmed"
        outcome = assess_outcome(
            req, obj, "pass", [], economics(obj, req, context=None), None
        )
        self.assertEqual(outcome["label"], "Подходит по проверенным условиям")
        outcome = assess_outcome(
            req, obj, "blocker", [], economics(obj, req, context=None), None
        )
        self.assertEqual(outcome["group"], "blocker")

    def test_comparison_pdf_and_live_recalculation_share_financial_outcome(self):
        req = self.profile("NSK-B01")
        obj = self.premises("NSK-P01")
        req.budget = Decimal(95000)
        req.save(update_fields=["budget"])
        link = RequestPremises.objects.create(request=req, premises=obj)
        before = evaluate(obj, self.user, req)
        report = issue_report(req, [obj], "", self.user, uuid4())
        original = bytes(report.pdf)
        for url in (
            reverse("compare", args=[req.pk]),
            reverse("premises", args=[req.pk, obj.pk]),
        ):
            response = self.client.get(url)
            self.assertContains(response, before.result["outcome"]["label"])
            self.assertContains(response, "28 000 ₽")
        pdf = "".join(
            page.extract_text() for page in PdfReader(BytesIO(original)).pages
        )
        self.assertIn("Технически подходит, но", pdf)
        self.assertIn("28 000", pdf)
        req.budget = Decimal(130000)
        req.version += 1
        req.save(update_fields=["budget", "version"])
        self.assertIsNone(current_evaluation(obj, req))
        card = next(card for card in self.results(req) if card["p"] == obj)
        self.assertEqual(card["outcome"]["monthly"]["difference"], "7000.00")
        obj.rent = Decimal(160000)
        obj.version += 1
        obj.save(update_fields=["rent", "version"])
        card = next(card for card in self.results(req) if card["p"] == obj)
        self.assertEqual(card["outcome"]["monthly"]["difference"], "43000.00")
        link.adaptation = link.other_once = link.equipment = link.initial_stock = (
            link.cash_reserve
        ) = Decimal(100000)
        link.save()
        card = next(card for card in self.results(req) if card["p"] == obj)
        self.assertEqual(
            [item["kind"] for item in card["outcome"]["financial_limits"]],
            ["monthly", "start"],
        )
        report.refresh_from_db()
        self.assertEqual(bytes(report.pdf), original)

    def test_short_and_detailed_pdf_share_preview_outcomes_and_keep_archive(self):
        req = self.profile("NSK-B01")
        req.budget = Decimal(95000)
        req.save(update_fields=["budget"])
        objects = [self.premises(code) for code in ("NSK-P01", "NSK-P11")]
        for obj in objects:
            RequestPremises.objects.create(request=req, premises=obj)
            evaluate(obj, self.user, req)
        legacy = Report.objects.create(
            request=req, version=1, snapshot={"layout_version": "presentation-v2"},
            pdf=b"%PDF-1.4\n% Historical file", fingerprint="a" * 64,
            creation_key=uuid4(), author=self.user,
        )
        original = bytes(legacy.pdf)
        url = reverse("compare", args=[req.pk])
        default = self.client.get(url).context["preview"]
        self.assertEqual(default["report_type"], "client_short")
        self.assertIn("Помещения превышают бюджет", default["client_content"]["summary"]["headlines"])
        self.assertEqual(len(default["objects"]), 2)

        data = {
            "objects": [p.pk for p in objects],
            "version": req.version, "selection_version": req.selection_version,
            "comment": "Обсудить смету. https://example.invalid/private-source",
            "conclusion": "reject", "action": "preview",
        }
        reports = {}
        for report_type in ("client_short", "broker_detailed"):
            with self.subTest(report_type=report_type):
                data.update(report_type=report_type, creation_key=str(uuid4()), action="preview")
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 200)
                preview = response.context["preview"]
                data["action"] = "issue"
                self.assertEqual(self.client.post(url, data).status_code, 302)
                report = Report.objects.get(creation_key=data["creation_key"])
                self.assertEqual(report.snapshot["client_content"], preview["client_content"])
                self.assertEqual(report.snapshot["sections"], preview["sections"])
                self.assertEqual(report.snapshot["broker_conclusion"]["code"], "reject")
                reports[report_type] = report
        short, detailed = reports.values()
        self.assertNotEqual(short.fingerprint, detailed.fingerprint)
        self.assertNotEqual(short.snapshot["layout_version"], detailed.snapshot["layout_version"])
        short_pdf = PdfReader(BytesIO(bytes(short.pdf)))
        self.assertEqual(len(short_pdf.pages), 4)
        first = " ".join(short_pdf.pages[0].extract_text().split())
        for text in ("95 000", "123 000", "268 000", "28 000", "173 000", "Технически подходит"):
            self.assertIn(text, first)
        short_text = "\n".join(page.extract_text() for page in short_pdf.pages)
        for text in ("private-source", "Будущий модуль", "http"):
            self.assertNotIn(text, short_text)
        self.assertIn("Заключение риелтора", short_text)
        self.assertGreater(len(PdfReader(BytesIO(bytes(detailed.pdf))).pages), len(short_pdf.pages))
        download = reverse("report_download", args=[legacy.pk])
        self.assertEqual(b"".join(self.client.get(download).streaming_content), original)
        self.client.force_login(self.other_user)
        for report in (legacy, short, detailed):
            self.assertEqual(
                self.client.get(reverse("report_download", args=[report.pk])).status_code, 404
            )

    def test_short_pdf_displays_blockers_questions_and_missing_costs(self):
        req = self.profile("NSK-B01")
        req.budget = Decimal(95000)
        req.save(update_fields=["budget"])
        objects = [self.premises(f"NSK-P{number:02d}") for number in (1, 11, 16, 19)]
        p01, _, p16, p19 = objects
        power = p16.observations.get(characteristic__code="power_kw")
        power.value = 2
        power.save(update_fields=["value"])
        p19.utilities = None
        p19.save(update_fields=["utilities"])
        for obj in objects:
            link = RequestPremises.objects.create(request=req, premises=obj)
            if obj == p01:
                link.adaptation, link.equipment, link.initial_stock = 40000, 60000, 20000
                link.other_once, link.cash_reserve = 0, 50000
                link.save()
            evaluate(obj, self.user, req)
        snapshot = report_snapshot(req, objects, "")
        summary = snapshot["client_content"]["summary"]
        self.assertIn("Есть критические технические ограничения", summary["headlines"])
        self.assertTrue(any("требует уточнения" in headline or "требуется уточнение" in headline for headline in summary["headlines"]))
        results = {obj["id"]: result for obj, result in zip(snapshot["objects"], snapshot["client_content"]["objects"])}
        self.assertTrue(results[p16.pk]["has_blocker"])
        self.assertTrue(any("Подтвердите у собственника" in action for action in results[p19.pk]["actions"]))
        self.assertIsNone(results[p19.pk]["monthly_rows"][2][1])
        self.assertEqual(Decimal(results[p01.pk]["opening_total"]), 280000)
        self.assertEqual(results[p01.pk]["break_even"], 1103)
        self.assertEqual([scenario["percent"] for scenario in results[p01.pk]["scenarios"]], ["0", "10", "20"])
        with self.assertRaisesMessage(ValueError, "хотя бы одно помещение"):
            report_snapshot(req, objects, "Все подходят", conclusion="recommend")
        for selection in ([p19], objects):
            report = issue_report(req, selection, "", self.user, uuid4())
            pages = PdfReader(BytesIO(bytes(report.pdf))).pages
            self.assertGreaterEqual(len(pages), 4)
            text = " ".join(" ".join(page.extract_text() for page in pages).split())
            self.assertIn("Подтвердите у собственника", text)
            self.assertIn("Полная сумма требует уточнения", text)
            if len(selection) == 4:
                self.assertIn("Есть критические технические ограничения", pages[0].extract_text())
                self.assertIn("Первоначальные закупки", text)
                self.assertIn("Снижение на 20%", text)

    def test_import_identity_idempotence_and_isolation(self):
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.counts["Premises", "created"], 20)
        self.assertEqual(self.counts["ClientRequest", "created"], 12)
        self.assertEqual(self.agency.premises.count(), 20)
        self.assertEqual(ClientRequest.objects.filter(agency=self.agency).count(), 12)
        self.assertFalse(self.agency.premises.exclude(city=showcase.CITY).exists())
        self.assertFalse(
            ClientRequest.objects.filter(agency=self.agency)
            .exclude(city=showcase.CITY)
            .exists()
        )
        self.assertEqual(self.agency.premises.values("district").distinct().count(), 10)
        before = list(self.agency.premises.order_by("pk").values())
        self.assertFalse(showcase.import_data(self.data))
        self.assertEqual(before, list(self.agency.premises.order_by("pk").values()))
        self.historical.refresh_from_db()
        self.assertEqual(self.historical.label, "История")
        self.assertFalse(RequestPremises.objects.exists())
        self.assertEqual(
            self.client.get(
                reverse("catalog_premises", args=[self.historical.pk])
            ).status_code,
            404,
        )
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(
                reverse("objects", args=[self.profile("NSK-B01").pk])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("catalog_premises", args=[self.premises("NSK-P01").pk])
            ).status_code,
            404,
        )

    def test_guard_and_atomic_rollback(self):
        with self.assertRaisesMessage(CommandError, "Нужны DEBUG=1"):
            call_command("seed_novosibirsk_showcase", stdout=StringIO())
        with self.assertRaisesMessage(CommandError, "отдельная база PostgreSQL"):
            showcase.require_demo_database(True)
        bad = deepcopy(self.data)
        bad["premises"][0]["label"] += " · изменение"
        bad["business_profiles"][-1]["monthly_budget_rub"] = -10
        with patch.object(showcase, "require_demo_database"), patch.object(
            showcase, "load_data", return_value=bad
        ):
            with self.assertRaisesMessage(CommandError, "Импорт отменён"):
                call_command(
                    "seed_novosibirsk_showcase", disposable_db=True, stdout=StringIO()
                )
        self.assertEqual(self.premises("NSK-P01").version, 1)
        self.assertEqual(
            self.premises("NSK-P01").label, self.data["premises"][0]["label"]
        )
        bad = deepcopy(self.data)
        bad["premises"][-1]["specs"]["water_supply"] = "да"
        with self.assertRaisesMessage(CommandError, "NSK-P20"):
            showcase.import_data(bad)

    def test_all_acceptance_cases_and_full_catalog_order(self):
        by_profile = {}
        for profile in self.data["business_profiles"]:
            req = self.profile(profile["external_id"])
            cards = self.results(req)
            self.assertEqual(len(cards), 20)
            keys = [
                (
                    outcome_priority(card),
                    budget_excess(card),
                    -card["score"] if card["score"] is not None else 1,
                )
                for card in cards
            ]
            self.assertEqual(keys, sorted(keys))
            by_profile[profile["external_id"]] = {
                card["p"].creation_key: card for card in cards
            }
        for case in self.data["acceptance_cases"]:
            with self.subTest(case=case):
                card = by_profile[case["profile"]][
                    showcase.stable_key(case["premises"])
                ]
                self.assertEqual(
                    card["status"], case["expect_technical"], case["reason"]
                )
        self.assertFalse(RequestPremises.objects.exists())
        p19 = by_profile["NSK-B01"][showcase.stable_key("NSK-P19")]
        self.assertIsNone(p19["power"])
        self.assertGreater(p19["evidence"]["unknown"], 0)
        self.assertGreater(p19["evidence"]["unconfirmed"], 0)
        self.assertFalse(p19["economics"]["complete"])
        p01 = by_profile["NSK-B01"][showcase.stable_key("NSK-P01")]
        self.assertEqual(p01["power"]["value"], 18)
        self.assertEqual(p01["status"], "pass")
        self.assertEqual(Decimal(p01["economics"]["gap"]), 8000)
        self.assertLess(p01["score"], 100)

    def test_shared_card_recalculates_across_profiles(self):
        obj = self.premises("NSK-P16")
        profiles = [self.profile(f"NSK-B{i:02}") for i in (1, 3, 4, 7)]

        def results():
            return [
                next(card for card in self.results(req) if card["p"] == obj)
                for req in profiles
            ]

        self.assertGreater(len({card["score"] for card in results()}), 1)
        reading = Observation.objects.get(premises=obj, characteristic__code="power_kw")
        reading.value = 6
        reading.save(update_fields=["value"])
        self.assertTrue(all(card["status"] == "blocker" for card in results()))
        self.assertEqual(self.agency.premises.count(), 20)
        self.assertFalse(RequestPremises.objects.exists())

    def test_comparison_finance_and_pdf(self):
        req = self.profile("NSK-B01")
        p01, p02 = map(self.premises, ("NSK-P01", "NSK-P02"))
        for obj in (p01, p01, p02):
            response = self.client.post(
                reverse("attach_recommendation", args=[req.pk, obj.pk]),
                {"version": req.version, "premises_version": obj.version},
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(req.premises_links.count(), 2)
        context = req.premises_links.get(premises=p01)
        cost = economics(p01, req, context=context)
        self.assertEqual(Decimal(cost["monthly_total"]), 123000)
        self.assertEqual(Decimal(cost["allowed_rent"]), 102000)
        self.assertTrue(financial_metrics(req, p01, context, cost)["opening_missing"])
        context.adaptation = 40000
        context.equipment = 60000
        context.initial_stock = 20000
        context.other_once = 0
        context.cash_reserve = 50000
        context.save()
        numbers = financial_metrics(req, p01, context, cost)
        self.assertEqual(numbers["break_even"], 1103)
        self.assertEqual(numbers["opening"], 280000)
        self.assertEqual(numbers["opening_missing"], [])
        self.assertEqual(numbers["scenarios"][-1]["profit"], Decimal("-50468.75"))
        req.refresh_from_db()
        for obj in (p01, p02):
            evaluate(obj, self.user, req)
        report = issue_report(req, [p01, p02], "Вымышленные данные", self.user, uuid4())
        pdf = self.client.get(reverse("report_download", args=[report.pk]))
        self.assertTrue(b"".join(pdf.streaming_content).startswith(b"%PDF-"))
        self.client.post(reverse("detach_premises", args=[req.pk, p01.pk]))
        self.assertEqual(list(req.premises.all()), [p02])
        self.assertTrue(Premises.objects.filter(pk=p01.pk).exists())
        self.assertTrue(Report.objects.filter(pk=report.pk).exists())
