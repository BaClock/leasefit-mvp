from datetime import datetime, timedelta, timezone as utc
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .listing_catalog import import_listings, refresh_publication
from .metrapi import MetrapiError, MetrapiQuota, get_payload, preview_item, rental_page
from .metrapi_limits import quota_paused
from .models import Agency, Characteristic, ClientRequest, Membership, ParserConfiguration, Premises, Requirement
from .parser_schedule import ensure_schedule, next_slot, run_due_discovery
from .services import economics
from .test_metrapi import LISTING


@override_settings(METRAPI_API_KEY="test-automation-key")
class AutomationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(name="Тестовое агентство")
        self.user = User.objects.create_user("automation-manager")
        Membership.objects.create(agency=self.agency, user=self.user, role="manager")
        self.client.force_login(self.user)
        ensure_schedule(self.agency.pk)

    def item(self, source_id="4567", **changes):
        return preview_item({
            **LISTING, "source_id": source_id, "url": f"https://www.avito.ru/commercial/{source_id}",
            "title": "Объявление о складе",
            "description": "Мощность 25 кВт. Доступ 24/7. Высота ворот 400 см.",
            "price": 45000, "price_unit": None, "price_period": "в месяц",
            "commercial_types": ["warehouse", "free_purpose"], "deposit": "Без залога",
            **changes,
        })

    def test_hourly_offsets_and_pause_preserves_disabled_category(self):
        configs = ParserConfiguration.objects.filter(agency=self.agency)
        self.assertEqual(list(configs.order_by("offset_minutes").values_list("offset_minutes", flat=True)), [0, 10, 20, 30, 40, 50])
        now = datetime(2026, 11, 28, 8, 14, tzinfo=utc.utc)
        self.assertEqual(next_slot(now, 60, 10).isoformat(), "2026-11-28T16:10:00+07:00")
        configs.filter(category="warehouse").update(enabled=False)
        endpoint = reverse("parser_schedule")
        self.client.post(endpoint, {"action": "pause"})
        configs.update(next_run_at=timezone.now() - timedelta(minutes=1))
        self.assertFalse(run_due_discovery())
        self.client.post(endpoint, {"action": "resume"})
        self.assertFalse(configs.get(category="warehouse").enabled)
        self.assertContains(self.client.get(reverse("parser")), "Расписание и категории")

    @patch("leasefit.parser_schedule.rental_page")
    def test_cursor_continues_after_error_and_switches_to_incremental_feed(self, page):
        config = self.agency.parser_configurations.get(category="warehouse")
        config.filters = {"maximum": 1, "districts": ["Центральный"], "rent_max": "50000"}
        config.save()

        def due():
            ParserConfiguration.objects.filter(pk=config.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
            self.assertTrue(run_due_discovery())
            config.refresh_from_db()

        page.side_effect = [
            ([self.item("1", district="Кировский")], "after-filtered-page", 1),
            ([self.item("2")], "after-import", 1),
            MetrapiError("Временная ошибка"),
            ([], None, 0),
            ([self.item("2", price=47500)], 27, 1),
            ([], 35, 0),
        ]
        due()
        self.assertEqual(config.cursor["position"], "after-import")
        self.assertEqual(page.call_args_list[0].args[0]["commercial_type"], ["warehouse"])
        self.assertEqual(config.runs.latest("pk").status, "partial")
        due()
        self.assertEqual(config.runs.latest("pk").status, "error")
        self.assertEqual(config.cursor["position"], "after-import")
        due()
        self.assertEqual(config.cursor, {"mode": "feed", "position": 27})
        self.assertEqual(Premises.objects.get().rent, Decimal("47500"))
        self.assertEqual(Premises.objects.get().publications.get().versions.count(), 2)
        due()
        self.assertEqual(config.cursor["position"], 35)
        self.assertIsNotNone(config.last_success_at)

    @patch("leasefit.metrapi.get_payload")
    def test_only_documented_category_query_and_archival_parameter(self, payload):
        payload.return_value = {"items": [{**LISTING, "delisted_at": "2026-11-26"}], "next_cursor": None}
        items, _, _ = rental_page({"commercial_type": ["warehouse", "retail"]}, include_inactive=True)
        query = parse_qs(urlsplit(payload.call_args.args[0]).query)
        self.assertEqual(query["commercial_type"], ["warehouse", "retail"])
        self.assertEqual(query["archived"], ["1"])
        self.assertFalse(items[0]["active"])

    @patch("leasefit.metrapi.read_payload")
    def test_quota_pauses_other_processes_using_key(self, read):
        with TemporaryDirectory() as directory, override_settings(METRAPI_STATE_DIRECTORY=directory, METRAPI_DAILY_LIMIT=1):
            read.return_value = (200, b"{}")
            self.assertEqual(get_payload("/items"), {})
            self.assertTrue(quota_paused())
            with self.assertRaises(MetrapiQuota):
                get_payload("/feed")
            read.assert_called_once()

    def test_specialized_extraction_and_structured_conflict_keeps_evidence(self):
        item = self.item(
            power_kw=15, description="Мощность 25 кВт. Вода и канализация подведены. Ворота высотой 400 см. Нагрузка на пол 2000 кг/м². Три кабинета. Газ подключен. Подъезд фуры.",
        )
        facts = item["facts"]
        self.assertTrue(facts["power_kw"]["conflicting"])
        self.assertEqual(facts["power_kw"]["value"], "15.00")
        self.assertEqual(facts["power_kw"]["extraction"]["fragments"], ["Мощность 25 кВт"])
        self.assertEqual(facts["room_count"]["value"], "3.00")
        self.assertEqual(facts["gate_height"]["value"], "4.00")
        self.assertEqual(facts["floor_load"]["value"], "2.00")
        for code in ("water_supply", "sewerage", "gas_supply", "semi_trailer_access"):
            self.assertTrue(facts[code]["boolean"])
        self.assertNotIn("phone", item["details"])

    def test_top_five_available_complete_and_fit_rules(self):
        profile = ClientRequest.objects.create(
            agency=self.agency, owner=self.user, title="Тестовый бизнес", budget=60000, districts=["Центральный"],
        )
        Requirement.objects.create(
            request=profile, characteristic=Characteristic.objects.get(code="power_kw"),
            name="Электрическая мощность", unit="кВт", minimum=20, operator="gte", priority="required",
        )
        item_set = [self.item(str(i), price=40000+i*1000) for i in range(1, 8)]
        item_set += [
            self.item("10", description="Мощность 5 кВт."),
            self.item("11", description="Напряжение 380 В."),
            self.item("12", price=65000),
            self.item("13", district="Кировский"),
            self.item("14"),
        ]
        imported = import_listings(self.agency.pk, self.user, item_set)
        for result in imported[:-1]:
            Premises.objects.filter(pk=result["premises"].pk).update(
                operating=0, utilities=0, other_monthly=0, vat_mode="included",
            )
        archived = imported[0]["publication"]
        refresh_publication(archived.pk, archived.checked_at, None, self.user)
        url = reverse("analysis", args=[profile.pk])
        page = self.client.get(url)
        self.assertEqual([card["p"].pk for card in page.context["top"]], [row["premises"].pk for row in imported[1:6]])
        self.assertTrue(all(card["score"] == 100 and card["preliminary"] for card in page.context["top"]))
        self.assertContains(page, "ТОП-5 помещений")
        for group in ("blocker", "technical_clarify", "finance_clarify", "over_budget"):
            self.assertEqual(page.context["recommendation_counts"][group], 1)
        archived.refresh_from_db()
        refresh_publication(archived.pk, archived.checked_at, item_set[0], self.user)
        self.assertEqual(self.client.get(url).context["top"][0]["p"].pk, archived.premises_id)
        self.assertEqual(archived.versions.count(), 3)

    def test_unknown_deposit_and_catalog_known_unknown_separation(self):
        rows = import_listings(self.agency.pk, self.user, [
            self.item("1", deposit=None, description="Есть вода."),
            self.item("2", description="Можно подключить воду."),
            self.item("3", description="Вода отсутствует."),
        ])
        self.assertEqual(economics(rows[0]["premises"])["rental_payment"], "45000.00")
        self.assertTrue(economics(rows[0]["premises"])["rental_deposit_unknown"])
        url = reverse("all_premises")
        self.assertEqual(len(self.client.get(url, {"water": "yes"}).context["cards"]), 1)
        cards = self.client.get(url, {"water": "yes", "include_unknown": "on"}).context["cards"]
        self.assertEqual([card["p"].catalog_unknown for card in cards], [False, True])

    def test_top_respects_different_business_profiles_and_shows_short_lists(self):
        variants = (
            ("food.coffee_to_go", "water_supply", "Есть вода.", "catering"),
            ("food.bakery", "separate_exhaust", "Есть отдельная вытяжка.", "manufacturing"),
            ("pickup.standard", "accessible_entrance", "Безбарьерный вход.", "retail"),
            ("office.internal", "heating", "Есть отопление.", "office"),
        )
        for index, (format_code, code, description, category) in enumerate(variants, 40):
            obj = import_listings(self.agency.pk, self.user, [self.item(
                str(index), description=description, heating=None, commercial_types=[category],
            )])[0]["premises"]
            Premises.objects.filter(pk=obj.pk).update(operating=0, utilities=0, other_monthly=0, vat_mode="included")
        for index, (format_code, code, _, _) in enumerate(variants, 40):
            with self.subTest(format=format_code):
                profile = ClientRequest.objects.create(
                    agency=self.agency, owner=self.user, title=format_code, business_format=format_code, budget=50000,
                )
                characteristic = Characteristic.objects.get(code=code)
                Requirement.objects.create(
                    request=profile, characteristic=characteristic, name=characteristic.label,
                    operator="eq", expected=True, priority="required",
                )
                url = reverse("analysis", args=[profile.pk])
                top = self.client.get(url).context["top"]
                self.assertEqual(len(top), 1)
                self.assertEqual(top[0]["p"].publications.get().source_id, str(index))
                self.assertContains(self.client.get(url, {"monthly_max": "40000"}), "Подходящих помещений по имеющимся данным пока нет.")
