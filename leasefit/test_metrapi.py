import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core import signing
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .metrapi import ListingUnavailable, MetrapiError, preview_item
from .forms import ObservationForm, PremisesForm
from .management.commands.refresh_metrapi_listings import shorten_existing_schedule
from .models import (
    Agency,
    Characteristic,
    ClientRequest,
    ListingPublication,
    ListingPublicationVersion,
    Membership,
    Observation,
    Premises,
    RequestPremises,
    Requirement,
)
from .services import check_requirement, economics


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
PHOTO = "https://10.img.avito.st/listings/photo.jpg?cqp=public"
LISTING_DESCRIPTION = "Сдаётся светлое помещение.\n\nВысота потолков 3 метра.\n<Замечание автора>"


def api_response(payload, status=200):
    response = BytesIO(json.dumps(payload).encode())
    response.status = status
    return response


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

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_import_preserves_source_financial_units_and_agency(self, conn):
        conn.return_value.getresponse.return_value = api_response({"items": [LISTING]})
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Открыть карточку в каталоге")
        self.assertNotContains(response, "CONTACT-NOT-FOR-IMPORT")
        conn.assert_called_once_with("api.metrapi.ru", timeout=10)
        request = conn.return_value.request.call_args
        self.assertEqual(request.kwargs["headers"]["X-Api-Key"], "mock-key")
        self.assertNotIn("Connection", request.kwargs["headers"])
        self.assertIn("realty_type=commercial&deal_type=rent", request.args[1])
        p = Premises.objects.get()
        publication = p.publications.get()
        self.assertEqual(response.context["results"][0]["premises_id"], p.pk)
        self.assertEqual((publication.source, publication.source_id), ("avito", "12345"))
        self.assertEqual(publication.versions.count(), 1)
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
            {"area_sqm", "floor", "ceiling_height_m", "separate_entrance", "heating"},
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
        conn.return_value.getresponse.return_value = api_response({"items": [LISTING]})
        self.client.post("/app/parser", self.search)
        p.refresh_from_db()
        self.assertEqual(p.label, "Название после проверки")
        self.assertEqual(Premises.objects.count(), 1)
        self.assertEqual(Observation.objects.count(), 5)
        self.assertEqual(publication.versions.count(), 1)

        token = signing.dumps(
            {"user": self.manager.pk, "agency": self.agency.pk, "item": preview_item(LISTING)},
            salt="leasefit.metrapi.import",
        )
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
        self.assertEqual(conn.return_value.request.call_count, 2)

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_rate_limit_and_invalid_import_do_not_create_properties(self, conn):
        conn.return_value.getresponse.return_value = api_response({}, status=429)
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        self.assertEqual(
            self.client.post("/app/parser/import", {"listing": "invalid"}).status_code,
            302,
        )
        self.assertFalse(Premises.objects.exists())

    @patch("leasefit.metrapi.sleep")
    @patch("leasefit.metrapi.HTTPSConnection")
    def test_short_rate_limit_retries_the_lookup_once(self, conn, sleep):
        def throttle():
            return api_response(
                {"detail": {"reason": "too_soon", "retry_after": 2}},
                status=429,
            )

        replies = conn.return_value.getresponse
        replies.side_effect = [throttle(), api_response({"items": [LISTING]})]
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Открыть карточку в каталоге")
        requests = conn.return_value.request.call_args_list
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        sleep.assert_called_once_with(2.1)

        replies.reset_mock(side_effect=True)
        sleep.reset_mock()
        replies.side_effect = [throttle(), throttle()]
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        self.assertEqual(replies.call_count, 2)
        sleep.assert_called_once()

    @patch("leasefit.metrapi.sleep")
    @patch("leasefit.metrapi.HTTPSConnection")
    def test_daily_quota_is_not_retried(self, conn, sleep):
        reply = conn.return_value.getresponse
        reply.return_value = api_response(
            {"detail": "daily limit exceeded"}, status=429
        )
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Достигнут лимит запросов Metrapi")
        reply.assert_called_once()
        sleep.assert_not_called()

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_unexpected_redirect_is_not_followed(self, conn):
        conn.return_value.getresponse.return_value = api_response({}, status=302)
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Metrapi вернул ошибку HTTP 302")
        conn.assert_called_once_with("api.metrapi.ru", timeout=10)
        conn.return_value.request.assert_called_once()
        conn.return_value.close.assert_called_once()

    def test_price_mapping_does_not_guess_missing_unit_periods(self):
        self.assertIsNone(preview_item({**LISTING, "price_period": None})["rate"])
        self.assertIsNone(preview_item({**LISTING, "price_unit": "за м³"})["rate"])
        self.assertEqual(
            preview_item({**LISTING, "price_unit": None, "price_period": None})["rent"],
            "18000.00",
        )

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_direct_lookup_imports_power_as_unverified_and_links_existing_card(
        self, conn
    ):
        item = {
            **LISTING,
            "description": "Отдельное помещение.\n· Электроснабжение: 25,5 кВт",
        }
        conn.return_value.getresponse.side_effect = [
            api_response({"item": item}), api_response({"item": item}),
        ]
        form = {
            "mode": "listing",
            "url": "https://www.avito.ru/novosibirsk/kommercheskaya_nedvizhimost/test_12345?tracking=example",
        }
        response = self.client.post("/app/parser", form)
        self.assertContains(response, "25.50 кВт")
        self.assertEqual(
            conn.return_value.request.call_args.args,
            ("GET", "/v1/item/avito/12345"),
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

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_direct_lookup_rejects_unrelated_hosts_and_non_rentals(self, conn):
        response = self.client.post(
            "/app/parser",
            {
                "mode": "listing",
                "url": "https://avito.ru.example.org/12345",
            },
        )
        self.assertContains(response, "Нужна прямая ссылка")
        conn.assert_not_called()
        conn.return_value.getresponse.return_value = api_response(
            {"item": {**LISTING, "deal_type": "sale"}}
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

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_keyword_import_fills_missing_characteristics_and_prioritizes_api_fields(self, conn):
        item = {
            **LISTING,
            "area_total": None, "floor": None, "ceiling_height": None,
            "water_supply": "Нет", "heating": None, "entrance_type": "Общий",
            "description": (
                "Площадь помещения: 48,5 м². Этаж: 2. Высота потолков 3 м.\n"
                "Вода и канализация. Приточная вентиляция. Отопление: центральное.\n"
                "Отдельный вход. Витринные окна. Электрическая мощность 15 кВт, есть кондиционер."
            ),
        }
        conn.return_value.getresponse.return_value = api_response({"items": [item]})
        preview = self.client.post("/app/parser", self.search)
        self.assertContains(preview, "Дополнительно из описания")
        self.assertContains(preview, "Проверка имеющихся карточек")
        p = Premises.objects.get()
        self.assertEqual((p.area, p.floor), (Decimal("48.50"), "2"))
        rows = {row.characteristic.code: row for row in p.observations.select_related("characteristic")}
        self.assertEqual(rows["ceiling_height_m"].value, 3)
        self.assertEqual(rows["power_kw"].value, 15)
        self.assertIs(rows["water_supply"].boolean, False)
        self.assertIs(rows["separate_entrance"].boolean, False)
        for code in ("sewerage", "ventilation", "supply_ventilation", "heating", "display_windows", "air_conditioning"):
            with self.subTest(code=code):
                self.assertTrue(rows[code].boolean)
                self.assertEqual((rows[code].status, rows[code].locator), ("unverified", "Описание объявления"))
        card = self.client.get(reverse("catalog_premises", args=[p.pk]))
        self.assertContains(card, "Ссылка на объявление ↗")
        self.assertNotContains(card, 'name="document"')

    def test_uncertain_text_does_not_create_characteristics(self):
        parsed = preview_item({
            "description": (
                "Площадь 30–50 м². Этаж 1/2. Есть вода. Воды нет.\n"
                "Можно сделать вентиляцию. Планируется отдельный вход.\n"
                "Парковка для соседнего здания. Возможно увеличение мощности до 40 кВт."
            )
        })
        self.assertEqual(parsed["facts"], {})

    def test_listing_confirmation_form_accepts_link_as_only_source_detail(self):
        p = Premises.objects.create(agency=self.agency, label="Лот")
        fields = {
            "characteristic": Characteristic.objects.get(code="power_kw").pk,
            "value": 30, "scope": "premises", "status": "unverified",
            "source_type": "listing", "source_url": LISTING["url"],
            "creation_key": uuid4(),
        }
        form = ObservationForm(fields, agency=self.agency, premises=p)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("document", form.fields)
        obj = form.save(commit=False)
        self.assertEqual((obj.source, obj.source_url), (LISTING["url"], LISTING["url"]))
        self.assertEqual(obj.observed_on, timezone.localdate())
        for invalid, field in (
            ({"source_url": ""}, "source_url"),
            ({"status": "confirmed"}, "source_type"),
            ({"source_type": "document", "status": "confirmed"}, "source_type"),
        ):
            test = ObservationForm({**fields, **invalid}, agency=self.agency, premises=p)
            self.assertFalse(test.is_valid())
            self.assertIn(field, test.errors)
        choices = dict(PremisesForm(instance=p).fields["commercial_source_type"].choices)
        self.assertNotIn("document", choices)

    @override_settings(METRAPI_REFRESH_HOURS=1)
    def test_worker_shortens_existing_daily_schedule_without_moving_retry(self):
        p = Premises.objects.create(agency=self.agency, label="Очередь")
        now = timezone.now()
        recent = ListingPublication.objects.create(
            agency=self.agency, premises=p, source="avito", source_id="1",
            checked_at=now - timedelta(minutes=15), next_check_at=now + timedelta(hours=23),
        )
        retry = ListingPublication.objects.create(
            agency=self.agency, premises=p, source="avito", source_id="2",
            checked_at=now - timedelta(hours=2), next_check_at=now + timedelta(minutes=30),
        )
        next_retry = retry.next_check_at
        self.assertEqual(shorten_existing_schedule(), 1)
        recent.refresh_from_db()
        retry.refresh_from_db()
        self.assertEqual(recent.next_check_at, recent.checked_at + timedelta(hours=1))
        self.assertEqual(retry.next_check_at, next_retry)

    def test_ceiling_height_uses_only_an_unambiguous_description_as_fallback(self):
        for description in (
            "Высота потолков от 3 м",
            "Высота потолков 3–4 м.",
            "Высота потолков 3 м – 4 м.",
            "Потолки 3 м. Высота потолков 4 м.",
            "Площадь 30 м², потолки под отделку.",
        ):
            with self.subTest(description=description):
                parsed = preview_item({**LISTING, "description": description, "ceiling_height": None})
                self.assertNotIn("ceiling_height_m", parsed["facts"])
        from_text = preview_item({**LISTING, "description": LISTING_DESCRIPTION, "ceiling_height": None})
        self.assertEqual(from_text["height"], "3.00")
        self.assertTrue(from_text["height_from_description"])
        self.assertEqual(from_text["facts"]["ceiling_height_m"]["locator"], "Описание объявления")
        structured = preview_item({**LISTING, "description": LISTING_DESCRIPTION})
        self.assertEqual(structured["height"], "3.40")
        self.assertFalse(structured["height_from_description"])

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_import_displays_listing_media_and_description_in_both_cards(self, conn):
        item = {
            **LISTING,
            "description": LISTING_DESCRIPTION,
            "ceiling_height": None,
            "photos": [
                PHOTO,
                PHOTO,
                "http://10.img.avito.st/insecure.jpg",
                "https://img.avito.st@unrelated.example/photo.jpg",
                "https://img.avito.st.invalid/photo.jpg",
                "https://127.0.0.1/photo.jpg",
                {"url": PHOTO},
            ],
        }
        conn.return_value.getresponse.return_value = api_response({"items": [item]})
        preview = self.client.post("/app/parser", self.search)
        self.assertContains(preview, "из описания, требует подтверждения")
        p = Premises.objects.get()
        self.assertEqual(p.listing.photos, [PHOTO])
        self.assertEqual(p.listing.description, LISTING_DESCRIPTION)
        height = p.observations.get(characteristic__code="ceiling_height_m")
        self.assertEqual((height.status, height.value, height.locator), (
            "unverified", Decimal("3"), "Описание объявления",
        ))
        req = ClientRequest.objects.create(title="Проверка", agency=self.agency, owner=self.manager)
        RequestPremises.objects.create(request=req, premises=p)
        for name, args in (("catalog_premises", [p.pk]), ("premises", [req.pk, p.pk])):
            page = self.client.get(reverse(name, args=args))
            self.assertContains(page, '<h3>Фото помещения</h3>', html=True)
            self.assertContains(page, PHOTO)
            self.assertContains(page, "Высота потолков 3 метра.")
            self.assertContains(page, "&lt;Замечание автора&gt;")
            height_entry = next(
                entry for group in page.context["groups"] for entry in group["entries"]
                if entry["characteristic"].code == "ceiling_height_m"
            )
            self.assertTrue(height_entry["primary"])
        self.client.force_login(self.create_user("foreign", Agency.objects.create(name="Other"), "manager"))
        self.assertEqual(self.client.get(reverse("catalog_premises", args=[p.pk])).status_code, 404)

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_group_import_links_sources_fills_missing_values_and_marks_conflicts(self, conn):
        first = {**LISTING, "group_id": "g-101", "group_spread_m": 12, "photos": [PHOTO]}
        second = {
            **first, "source": "cian", "source_id": "22345", "group_spread_m": 12,
            "url": "https://www.cian.ru/rent/commercial/22345/",
            "price": 120000, "price_unit": None, "price_period": "в месяц",
            "ceiling_height": "4.20", "commission": "4 000 руб.",
            "photos": ["https://images.cdn-cian.ru/picture.jpg"],
        }
        conn.return_value.getresponse.return_value = api_response({"items": [first, second]})
        response = self.client.post("/app/parser", self.search)
        self.assertContains(response, "Новых помещений — 1")
        self.assertNotContains(response, "Проверьте данные и выберите объявление")
        p = Premises.objects.get()
        self.assertEqual(p.commission, 4000)
        self.assertEqual(len(p.listing.photos), 2)
        self.assertEqual(p.publications.count(), 2)
        self.assertEqual(ListingPublicationVersion.objects.count(), 2)
        self.assertEqual(
            {row["key"] for row in p.listing_conflicts}, {"rent", "ceiling_height_m"},
        )
        self.assertTrue(economics(p)["rent_conflict"])
        card = self.client.get(reverse("catalog_premises", args=[p.pk]))
        self.assertContains(card, "Требует уточнения")
        self.assertContains(card, "История версий", count=2)
        self.assertContains(card, "120000.00")

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_unreliable_group_keeps_separate_cards_and_agencies(self, conn):
        first = {**LISTING, "group_id": "g", "group_spread_m": 125}
        second = {
            **first, "source": "cian", "source_id": "777",
            "url": "https://www.cian.ru/rent/commercial/777/",
        }
        conn.return_value.getresponse.return_value = api_response({"items": [first, second]})
        self.client.post("/app/parser", self.search)
        self.assertEqual(Premises.objects.count(), 2)
        other = Agency.objects.create(name="Другой каталог")
        self.client.force_login(self.create_user("other-catalog", other, "manager"))
        conn.return_value.getresponse.return_value = api_response({"items": [first]})
        self.client.post("/app/parser", self.search)
        self.assertEqual(Premises.objects.filter(agency=other).count(), 1)
        origin = ListingPublication.objects.filter(agency=self.agency).first()
        url = reverse("publication_history", args=[origin.premises_id, origin.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_each_meaningful_change_creates_a_version_and_preserves_manual_updates(self, conn):
        first = {**LISTING, "price": 80000, "price_unit": None, "price_period": "в месяц"}
        conn.return_value.getresponse.return_value = api_response({"items": [first]})
        self.client.post("/app/parser", self.search)
        p = Premises.objects.get()
        pub = p.publications.get()
        date_only = {**first, "updated_at": "2026-01-02T09:00:00+07:00"}
        conn.return_value.getresponse.return_value = api_response({"items": [date_only]})
        self.client.post("/app/parser", self.search)
        self.assertEqual(pub.versions.count(), 1)
        updated = {**first, "price": 85000}
        conn.return_value.getresponse.return_value = api_response({"items": [updated]})
        self.client.post("/app/parser", self.search)
        p.refresh_from_db()
        self.assertEqual(p.rent, 85000)
        p.rent = 91000
        p.save(update_fields=["rent"])
        conn.return_value.getresponse.return_value = api_response({"items": [{**updated, "price": 87000}]})
        self.client.post("/app/parser", self.search)
        p.refresh_from_db()
        self.assertEqual(p.rent, 91000)
        self.assertEqual(
            list(pub.versions.values_list("snapshot__rent", flat=True)),
            ["87000.00", "85000.00", "80000.00"],
        )
        response = self.client.get(reverse("publication_history", args=[p.pk, pub.pk]))
        self.assertContains(response, "Версия 1")
        self.assertContains(response, "Версия 3")

    @patch("leasefit.management.commands.refresh_metrapi_listings.find_rental")
    @patch("leasefit.metrapi.HTTPSConnection")
    def test_periodic_recheck_passes_through_temporary_failure_and_records_removal(self, conn, find):
        conn.return_value.getresponse.return_value = api_response({"items": [LISTING]})
        self.client.post("/app/parser", self.search)
        pub = ListingPublication.objects.get()
        call_command("refresh_metrapi_listings", once=True, stdout=StringIO())
        find.assert_not_called()
        pub.next_check_at = timezone.now() - timedelta(minutes=1)
        pub.save(update_fields=["next_check_at"])
        find.side_effect = MetrapiError("Temporary")
        call_command("refresh_metrapi_listings", once=True, stdout=StringIO(), stderr=StringIO())
        pub.refresh_from_db()
        self.assertGreater(pub.next_check_at, timezone.now())
        self.assertEqual(pub.version, 1)
        pub.next_check_at = timezone.now() - timedelta(minutes=1)
        pub.save(update_fields=["next_check_at"])
        find.side_effect = ListingUnavailable("Missing")
        call_command("refresh_metrapi_listings", once=True, stdout=StringIO())
        pub.refresh_from_db()
        self.assertEqual(pub.version, 2)
        self.assertEqual(pub.snapshot["state"], "unavailable")
        self.assertEqual(pub.versions.get(number=1).snapshot["state"], "active")
        self.assertTrue(Premises.objects.filter(pk=pub.premises_id).exists())
        pub.next_check_at = timezone.now() - timedelta(minutes=1)
        pub.save(update_fields=["next_check_at"])
        find.side_effect = None
        find.return_value = preview_item(LISTING)
        call_command("refresh_metrapi_listings", once=True, stdout=StringIO())
        pub.refresh_from_db()
        self.assertEqual(pub.version, 3)
        self.assertEqual(pub.snapshot["state"], "active")

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_refresh_switches_automatically_managed_price_basis(self, conn):
        conn.return_value.getresponse.return_value = api_response({"items": [LISTING]})
        self.client.post("/app/parser", self.search)
        p = Premises.objects.get()
        self.assertEqual(p.rate, 1500)
        conn.return_value.getresponse.return_value = api_response({
            "items": [{**LISTING, "price": 95000, "price_unit": None, "price_period": "в месяц"}]
        })
        self.client.post("/app/parser", self.search)
        p.refresh_from_db()
        self.assertEqual(p.rent, 95000)
        self.assertIsNone(p.rate)

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_refresh_existing_listing_preserves_confirmed_height_and_card(self, conn):
        p = Premises.objects.create(
            agency=self.agency, label="Ручное название", rent=100000, listing_url=LISTING["url"],
            commercial_source="Metrapi / Авито / 12345",
        )
        height = Observation.objects.create(
            premises=p, characteristic=Characteristic.objects.get(code="ceiling_height_m"),
            name="Высота потолков", unit="м", value=4, source_type="owner", source="Собственник",
            observed_on="2026-11-28", status="confirmed", confirmation="Актуальный ответ",
            author=self.manager,
        )
        item = {**LISTING, "description": LISTING_DESCRIPTION, "photos": [PHOTO]}
        conn.return_value.getresponse.return_value = api_response({"item": item})
        url = reverse("catalog_listing_refresh", args=[p.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("catalog_premises", args=[p.pk]))
        p.refresh_from_db()
        self.assertEqual((p.label, p.rent, p.version), ("Ручное название", Decimal("100000"), 1))
        self.assertEqual(list(p.observations.all()), [height])
        self.assertEqual(p.listing.photos, [PHOTO])
        realtor = self.create_user("ordinary", self.agency, "realtor")
        self.client.force_login(realtor)
        self.assertEqual(self.client.post(url).status_code, 403)

    @patch("leasefit.metrapi.HTTPSConnection")
    def test_refresh_old_import_fills_missing_height_once(self, conn):
        p = Premises.objects.create(
            agency=self.agency, label="Ранее импортированное помещение",
            listing_url=LISTING["url"], commercial_source="Metrapi / Авито / 12345",
        )
        item = {**LISTING, "description": LISTING_DESCRIPTION, "photos": [PHOTO], "ceiling_height": None}
        conn.return_value.getresponse.side_effect = [api_response({"item": item}), api_response({"item": item})]
        url = reverse("catalog_listing_refresh", args=[p.pk])
        for _ in range(2):
            self.assertEqual(self.client.post(url).status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.version, 2)
        self.assertEqual(p.observations.get(characteristic__code="ceiling_height_m").value, 3)
        self.assertEqual(p.listing.description, LISTING_DESCRIPTION)
