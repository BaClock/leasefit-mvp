import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .metrapi import preview_item
from .models import (
    Agency,
    Characteristic,
    Membership,
    Observation,
    Premises,
    Requirement,
)
from .services import check_requirement


LISTING = {
    "source": "avito",
    "source_id": "12345",
    "deal_type": "rent",
    "realty_type": "commercial",
    "url": "https://www.avito.ru/commercial/12345",
    "address": "Новосибирск, Учебная улица, 1",
    "locality": "Новосибирск",
    "district": "Центральный",
    "area_total": 55.2,
    "floor": 1,
    "price": 18000,
    "price_unit": "за м²",
    "price_period": "в год",
    "commercial_types": ["free_purpose", "retail"],
    "ceiling_height": 3.4,
    "entrance_type": "Общий",
    "deposit": "63 000 ₽",
    "commission": "50%",
    "tax_form": "НДС не включен",
    "heating": "Центральное",
    "phone": "CONTACT-NOT-FOR-IMPORT",
    "description": "Текст с неподтверждённой мощностью 50 кВт.",
}


@override_settings(METRAPI_API_KEY="mock-key")
class MetrapiTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(name="Agency A")
        self.manager = self.create_user("manager", self.agency, "manager")
        self.client.force_login(self.manager)
        self.search = {"locality": "Новосибирск", "limit": 5}

    def create_user(self, name, agency, role):
        user = User.objects.create_user(name)
        Membership.objects.create(user=user, agency=agency, role=role)
        return user

    @patch("leasefit.metrapi.build_opener")
    def test_import_preserves_source_financial_units_and_agency(self, opener):
        opener.return_value.open.return_value = BytesIO(
            json.dumps({"items": [LISTING]}).encode()
        )
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Добавить в каталог")
        self.assertNotContains(response, "CONTACT-NOT-FOR-IMPORT")
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.host, "api.metrapi.ru")
        self.assertEqual(request.get_header("X-api-key"), "mock-key")
        self.assertIn("realty_type=commercial&deal_type=rent", request.full_url)
        token = response.context["results"][0]["token"]
        self.assertFalse(Premises.objects.exists())
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": token}).status_code, 302
        )
        p = Premises.objects.get()
        self.assertEqual(p.agency, self.agency)
        self.assertEqual((p.city, p.district), ("Новосибирск", "Центральный"))
        self.assertFalse(p.location_confirmed)
        self.assertEqual(p.rate, Decimal("1500.00"))
        self.assertIsNone(p.rent)
        self.assertEqual(p.deposit, 63000)
        self.assertIsNone(p.commission)
        self.assertIsNone(p.vat_rate)
        self.assertEqual(p.vat_mode, "excluded")
        self.assertEqual(p.premises_type, "retail")
        self.assertEqual(p.commercial_status, "unverified")
        self.assertEqual(p.commercial_source_type, "listing")
        observations = p.observations.all()
        self.assertEqual(
            set(observations.values_list("characteristic__code", flat=True)),
            {"area_sqm", "floor", "ceiling_height_m", "separate_entrance"},
        )
        self.assertTrue(
            all(
                o.status == "unverified" and o.source_type == "listing"
                for o in observations
            )
        )
        self.assertIs(
            observations.get(characteristic__code="separate_entrance").boolean, False
        )
        p.label = "Название после проверки"
        p.save()
        self.client.post("/app/parser/import", {"listing": token})
        p.refresh_from_db()
        self.assertEqual(p.label, "Название после проверки")
        self.assertEqual(Premises.objects.count(), 1)
        self.assertEqual(Observation.objects.count(), 4)

        other = Agency.objects.create(name="Agency B")
        self.client.force_login(self.create_user("foreign-manager", other, "manager"))
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": token}).status_code, 403
        )
        self.client.force_login(self.create_user("realtor", self.agency, "realtor"))
        self.assertEqual(self.client.post("/app/parser", self.search).status_code, 403)
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": token}).status_code, 403
        )
        self.assertEqual(opener.return_value.open.call_count, 1)

    @patch("leasefit.metrapi.build_opener")
    def test_rate_limit_and_invalid_import_do_not_create_properties(self, opener):
        opener.return_value.open.side_effect = HTTPError(
            "https://api.metrapi.ru/v1/items", 429, "Too Many Requests", {}, None
        )
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": "invalid"}).status_code,
            302,
        )
        self.assertFalse(Premises.objects.exists())

    @patch("leasefit.metrapi.sleep")
    @patch("leasefit.metrapi.build_opener")
    def test_short_rate_limit_retries_the_lookup_once(self, opener, sleep):
        def throttle():
            return HTTPError(
                "https://api.metrapi.ru/v1/items",
                429,
                "Too Many Requests",
                {},
                BytesIO(b'{"detail":{"reason":"too_soon","retry_after":2}}'),
            )

        call = opener.return_value.open
        call.side_effect = [throttle(), BytesIO(json.dumps({"items": [LISTING]}).encode())]
        self.assertContains(self.client.post("/app/parser", self.search), "Добавить в каталог")
        self.assertEqual(call.call_count, 2)
        self.assertIs(call.call_args_list[0].args[0], call.call_args_list[1].args[0])
        sleep.assert_called_once_with(2.1)

        call.reset_mock(side_effect=True)
        sleep.reset_mock()
        call.side_effect = [throttle(), throttle()]
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once()

    @patch("leasefit.metrapi.sleep")
    @patch("leasefit.metrapi.build_opener")
    def test_daily_quota_is_not_retried(self, opener, sleep):
        call = opener.return_value.open
        call.side_effect = HTTPError(
            "https://api.metrapi.ru/v1/items",
            429,
            "Too Many Requests",
            {},
            BytesIO(b'{"detail":"daily limit exceeded"}'),
        )
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        call.assert_called_once()
        sleep.assert_not_called()

    def test_price_mapping_does_not_guess_missing_unit_periods(self):
        self.assertIsNone(preview_item({**LISTING, "price_period": None})["rate"])
        self.assertIsNone(preview_item({**LISTING, "price_unit": "за м³"})["rate"])
        self.assertEqual(
            preview_item({**LISTING, "price_unit": None, "price_period": None})["rent"],
            "18000.00",
        )

    @patch("leasefit.metrapi.build_opener")
    def test_direct_lookup_imports_power_as_unverified_and_links_existing_card(
        self, opener
    ):
        item = {
            **LISTING,
            "description": "Отдельное помещение.\n· Электроснабжение: 25,5 кВт",
        }
        reply = json.dumps({"item": item}).encode()
        opener.return_value.open.side_effect = [BytesIO(reply), BytesIO(reply)]
        form = {
            "mode": "listing",
            "url": "https://www.avito.ru/novosibirsk/kommercheskaya_nedvizhimost/test_12345?tracking=example",
        }
        response = self.client.post("/app/parser", form)
        self.assertContains(response, "25.50 кВт")
        self.assertEqual(
            opener.return_value.open.call_args.args[0].full_url,
            "https://api.metrapi.ru/v1/item/avito/12345",
        )
        token = response.context["results"][0]["token"]
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": token}).status_code, 302
        )
        p = Premises.objects.get()
        power = p.observations.get(characteristic__code="power_kw")
        self.assertEqual(power.value, Decimal("25.50"))
        self.assertEqual((power.status, power.source_type), ("unverified", "listing"))
        requirement = Requirement(
            characteristic=Characteristic.objects.get(code="power_kw"),
            name="Мощность",
            unit="кВт",
            minimum=15,
            operator="gte",
            priority="required",
        )
        self.assertEqual(check_requirement(requirement, p)["status"], "clarify")
        response = self.client.post("/app/parser", form)
        self.assertContains(response, "Открыть карточку в каталоге")
        self.assertNotContains(response, "Добавить в каталог")

    @patch("leasefit.metrapi.build_opener")
    def test_direct_lookup_rejects_unrelated_hosts_and_non_rentals(self, opener):
        response = self.client.post(
            "/app/parser",
            {
                "mode": "listing",
                "url": "https://avito.ru.example.org/12345",
            },
        )
        self.assertContains(response, "Нужна прямая ссылка")
        opener.assert_not_called()
        opener.return_value.open.return_value = BytesIO(
            json.dumps({"item": {**LISTING, "deal_type": "sale"}}).encode()
        )
        response = self.client.post(
            "/app/parser", {"mode": "listing", "url": LISTING["url"]}
        )
        self.assertContains(response, "действующим предложением коммерческой аренды")
        self.assertNotContains(response, "Добавить в каталог")

    def test_power_extraction_leaves_ambiguous_values_unknown(self):
        for description in (
            "Мощность: 10–25 кВт",
            "Выделено до 30 кВт",
            "Электричество: 12 кВт. Электроснабжение: 20 кВт.",
            "На помещение можно выделить 30 кВт",
            "Электроснабжение: 50 кВт на всё здание.",
            "Требуется мощность: 30 кВт",
        ):
            with self.subTest(description=description):
                self.assertIsNone(
                    preview_item({**LISTING, "description": description})["power"]
                )
        self.assertEqual(
            preview_item({**LISTING, "description": "⚡ Mощность — 25 КBт"})["power"],
            "25.00",
        )
