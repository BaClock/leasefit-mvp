import hashlib, json, uuid
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from .models import *
from .services import *
from .forms import RequirementForm, PilotForm, ObservationForm, AttachmentForm
from pypdf import PdfReader


def make_premises(request, **values):
    contextual = {
        k: values.pop(k)
        for k in ["adaptation", "other_once", "stress", "assumptions", "internal_notes"]
        if k in values
    }
    p = Premises.objects.create(agency=request.agency, **values)
    RequestPremises.objects.create(request=request, premises=p, **contextual)
    return p


def grouped_payload(data, req):
    from .forms import GroupedRequirementsForm

    grouped = GroupedRequirementsForm(request_obj=req)
    result = dict(data)
    for key in grouped.fields:
        result.setdefault(
            key, grouped[key].value() if grouped[key].value() is not None else ""
        )
    if data.get("requirements-0-name") == "Мощность":
        result.update(
            power_kw_min=data.get("requirements-0-minimum"),
            power_kw_priority=data.get("requirements-0-priority", "required"),
        )
    result.update({"custom-TOTAL_FORMS": "0", "custom-INITIAL_FORMS": "0"})
    return result


class AcceptanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.agency = Agency.objects.create(name="Условное агентство А")
        cls.other_agency = Agency.objects.create(name="Условное агентство Б")

        def member(name, agency, role="realtor"):
            u = User.objects.create_user(
                username=name, email=name, password="Strong-test-password-942!"
            )
            Membership.objects.create(user=u, agency=agency, role=role)
            return u

        cls.owner = member("agent@example.invalid", cls.agency)
        cls.colleague = member("colleague@example.invalid", cls.agency)
        cls.manager = member("leader@example.invalid", cls.agency, "manager")
        cls.outsider = member("outsider@example.invalid", cls.other_agency, "manager")
        cls.req = ClientRequest.objects.create(
            title="Условная кофейня",
            agency=cls.agency,
            owner=cls.owner,
            budget=70000,
            start_budget=180000,
            stage="active",
        )
        cls.power = Requirement.objects.create(
            request=cls.req, name="Мощность", unit="кВт", minimum=15
        )
        cls.p = make_premises(
            request=cls.req,
            label="Учебный объект А",
            area=60,
            rate=1000,
            operating=8000,
            utilities=5000,
            other_monthly=0,
            vat_mode="included",
            deposit=60000,
            commission=20000,
            adaptation=90000,
            other_once=0,
            stress=5000,
            internal_notes="SECRET_INTERNAL_NOTE",
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def observe(self, p=None, **kw):
        d = dict(
            premises=p or self.p,
            name="Мощность",
            value=15,
            unit="кВт",
            source_type="document",
            source="Условный акт",
            observed_on=timezone.localdate(),
            status="confirmed",
            confirmation="Проверено по акту для помещения",
            author=self.owner,
        )
        d.update(kw)
        return Observation.objects.create(**d)

    def result(self):
        return evaluate(self.p, self.owner, self.req).result

    def issue(self, objects=None, key=None, comment=""):
        return issue_report(
            self.req, objects or [self.p], comment, self.owner, key or uuid.uuid4()
        )

    def test_12_kw_blocker(self):
        self.observe(value=12)
        self.assertEqual(self.result()["status"], "blocker")

    def test_exact_15_kw_passes(self):
        self.observe()
        self.assertEqual(self.result()["status"], "pass")

    def test_premises_ranking_uses_current_evidence_budget_and_status_order(self):
        self.observe()
        affordable = {
            "rent": 59000,
            "operating": 0,
            "utilities": 0,
            "other_monthly": 0,
            "vat_mode": "included",
        }
        confirmed = make_premises(
            self.req, label="Подходит по профилю и бюджету", **affordable
        )
        unknown = make_premises(
            self.req, label="Нужно подтвердить мощность", **affordable
        )
        blocked = make_premises(self.req, label="Мощности недостаточно", **affordable)
        self.observe(p=confirmed)
        self.observe(p=unknown, source_type="listing")
        self.observe(p=blocked, value=12)
        catalog_only = Premises.objects.create(agency=self.agency, label="Только в каталоге")
        url = reverse("objects", args=[self.req.pk])
        response = self.client.get(url)
        self.assertEqual(
            [
                (card["p"].pk, card["status"], card["score"])
                for card in response.context["cards"]
            ],
            [
                (confirmed.pk, "pass", 100),
                (self.p.pk, "pass", 60),
                (unknown.pk, "clarify", None),
                (blocked.pk, "blocker", 40),
            ],
        )
        self.assertContains(
            response, '<details class="assessment-details" open>', count=2
        )
        self.assertContains(response, "1 критическое несоответствие", count=2)
        self.assertContains(response, "Почему помещение не подходит")
        self.assertContains(response, "Почему помещение подходит")
        self.assertContains(response, "Что нужно уточнить")
        self.assertContains(response, "Превышение 3 000 ₽")
        self.assertContains(response, "12.00 кВт")
        self.assertContains(response, "Выбрать из каталога")
        self.assertContains(response, "Создать новое помещение")
        self.assertContains(response, catalog_only.label)
        self.assertNotIn(catalog_only.pk, [card["p"].pk for card in response.context["cards"]])
        self.assertTrue(
            response.context["compare_url"].split("?")[1].startswith(
                f"objects={confirmed.pk}&objects={self.p.pk}"
            )
        )
        self.assertFalse(Evaluation.objects.filter(request=self.req).exists())

        unknown.observations.update(superseded=True)
        self.observe(p=unknown)
        self.assertEqual(self.client.get(url).context["cards"][1]["p"], unknown)

    def test_assessment_details_prioritize_blockers_in_both_lists(self):
        self.p.city = self.req.city
        self.p.district = "Ленинский"
        self.p.location_confirmed = True
        self.p.save(update_fields=["city", "district", "location_confirmed"])
        self.observe(value=12)
        for code in ("water_supply", "sewerage"):
            char = Characteristic.objects.get(code=code)
            Requirement.objects.create(
                request=self.req, characteristic=char, name=char.label, operator="eq"
            )
            if code == "water_supply":
                self.observe(
                    characteristic=char, name=char.label, unit="", value=None, boolean=True
                )
        Requirement.objects.create(
            request=self.req,
            characteristic=Characteristic.objects.get(code="sewerage"),
            name="Дополнительное условие о канализации",
            operator="eq",
            priority="desired",
        )
        response = self.client.get(
            reverse("objects", args=[self.req.pk]), {"status": "blocker"}
        )
        self.assertContains(response, '<details class="recommendation-blockers" open>')
        self.assertContains(response, "1 критическое несоответствие", count=2)
        self.assertContains(response, "1 параметр требует уточнения", count=2)
        self.assertContains(response, "1 требование выполнено", count=2)
        groups = response.content.decode().split('<div class="assessment-sections">')[1:]
        self.assertEqual(len(groups), 2)
        for group in groups:
            order = [
                group.index(f"<h5>{label}</h5>")
                for label in (
                    "Критические несоответствия",
                    "Что нужно уточнить",
                    "Почему помещение подходит",
                )
            ]
            self.assertEqual(order, sorted(order))

    def test_suitability_rating_counts_each_characteristic_once(self):
        rows = [
            {"characteristic_id": 1, "priority": "required", "status": "pass"},
            {"characteristic_id": 1, "priority": "required", "status": "blocker"},
            {"characteristic_id": 1, "priority": "desired", "status": "pass"},
            {"characteristic_id": 2, "priority": "desired", "status": "pass"},
            {"characteristic_id": 3, "priority": "desired", "status": "clarify"},
        ]
        for index, row in enumerate(rows):
            row["code"] = str(row["characteristic_id"])
            row["requirement"] = f"Условие {index}"
        score, improvements = suitability_rating(rows, True)
        self.assertEqual(score, 43)
        self.assertEqual(
            [(item["requirements"], item["gain"]) for item in improvements],
            [(["Условие 1"], 43), (["Условие 4"], 14)],
        )
        self.assertEqual(suitability_rating(rows, None)[0], 20)
        self.assertEqual(suitability_rating(rows[:2], None)[0], 0)
        self.assertEqual(suitability_rating(rows[2:], True), (None, []))
        score, improvements = suitability_rating([{**rows[0], "status": "clarify"}], True)
        self.assertIsNone(score)
        self.assertIsNone(improvements[0]["gain"])

    def test_ranking_shows_exact_gains_and_links_to_editable_fields(self):
        self.observe()
        Requirement.objects.create(
            request=self.req, name="Отдельная вытяжка", operator="eq", expected=True
        )
        self.observe(name="Отдельная вытяжка", value=None, boolean=True, unit="")
        for name in ["Водоснабжение", "Канализация", "Вентиляция"]:
            Requirement.objects.create(
                request=self.req, name=name, operator="eq", expected=True, priority="desired"
            )
        url = reverse("objects", args=[self.req.pk])
        response = self.client.get(url)
        card = response.context["cards"][0]
        self.assertEqual(card["score"], 55)
        self.assertEqual([item["gain"] for item in card["improvements"]], [18, 9, 9, 9])
        self.assertContains(response, "Что повысит балл")
        self.assertContains(response, "Снизьте расходы на 3 000 ₽ в месяц.")
        self.assertContains(response, "Цель — до 70 000 ₽ в месяц.")
        self.assertContains(response, "?characteristic=water_supply#evidence-form")
        self.assertContains(response, "?edit=commercial#commercial-terms")
        premises_url = reverse("premises", args=[self.req.pk, self.p.pk])
        self.assertContains(
            self.client.get(premises_url + "?edit=commercial"),
            '<details id="commercial-terms" open>',
        )
        self.observe(name="Водоснабжение", value=None, boolean=True, unit="")
        revised = self.client.get(url).context["cards"][0]
        self.assertEqual(revised["score"], 64)
        self.assertNotIn("water_supply", [item.get("code") for item in revised["improvements"]])

    def test_observation_confirmation_requires_a_value_in_the_matching_field(self):
        url = reverse("observation_add", args=[self.req.pk, self.p.pk])
        data = {
            "scope": "premises",
            "source_type": "owner",
            "source": "Письмо собственника",
            "observed_on": timezone.localdate(),
            "status": "confirmed",
            "version": self.p.version,
            "creation_key": uuid.uuid4(),
        }
        for code, field, value in [
            ("water_supply", "boolean", "false"),
            ("power_kw", "value", "0"),
            ("floor", "text", "1 этаж"),
        ]:
            with self.subTest(code=code):
                characteristic = Characteristic.objects.get(code=code)
                response = self.client.post(
                    url, {**data, "characteristic": characteristic.pk}
                )
                self.assertEqual(response.status_code, 400)
                errors = response.context["obsform"].errors
                self.assertEqual(set(errors), {field, "confirmation"})
                self.assertFalse(
                    self.p.observations.filter(characteristic=characteristic).exists()
                )
                saved = self.client.post(
                    url,
                    {
                        **data,
                        "characteristic": characteristic.pk,
                        field: value,
                        "confirmation": "Проверено по письменному ответу",
                    },
                )
                self.assertRedirects(
                    saved, reverse("premises", args=[self.req.pk, self.p.pk])
                )
                observation = self.p.observations.get(characteristic=characteristic)
                self.assertEqual(observation.status, "confirmed")
                expected_value = {"boolean": False, "value": 0}.get(field, value)
                self.assertEqual(getattr(observation, field), expected_value)
                self.p.refresh_from_db()
                data.update(version=self.p.version, creation_key=uuid.uuid4())

        water = Characteristic.objects.get(code="water_supply")
        self.assertEqual(
            self.client.post(
                url, {**data, "status": "unknown", "characteristic": water.pk}
            ).status_code,
            302,
        )
        self.assertIsNone(
            self.p.observations.get(characteristic=water, status="unknown").boolean
        )

    def test_listing_cannot_confirm_even_if_imported_as_confirmed(self):
        self.observe(source_type="listing")
        self.assertEqual(self.result()["status"], "clarify")

    def test_unknown_is_not_zero(self):
        self.assertEqual(self.result()["status"], "clarify")
        self.observe(value=0)
        self.assertEqual(self.result()["status"], "blocker")

    def test_conflicting_sources_need_resolution(self):
        self.observe(
            value=12, source_type="listing", status="unverified", confirmation=""
        )
        o = self.observe(value=15)
        self.assertEqual(self.result()["status"], "clarify")
        response = self.client.post(
            f"/app/requests/{self.req.pk}/objects/{self.p.pk}/observations/{o.pk}/resolve",
            {
                "version": 1,
                "reason": "Актуальный акт собственника; объявление ошибочно",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.p.refresh_from_db()
        self.assertEqual(self.result()["status"], "pass")
        self.assertEqual(self.p.observations.filter(superseded=True).count(), 1)

    def test_archive_building_and_room_never_confirm_premises(self):
        for scope in ["premises", "building", "room", "reference"]:
            with self.subTest(scope=scope):
                self.p.observations.all().delete()
                self.observe(
                    scope=scope,
                    source_type="archive",
                    observed_on=timezone.localdate() - timedelta(days=3000),
                )
                result = self.result()
                self.assertEqual(result["status"], "clarify")
                self.assertIn(
                    str(timezone.localdate() - timedelta(days=3000)),
                    result["rows"][0]["value"],
                )
        self.p.observations.all().delete()
        self.observe(scope="building", boolean=True)
        self.assertEqual(self.result()["status"], "clarify")

    def test_desired_does_not_create_blocker(self):
        self.observe()
        r = Requirement.objects.create(
            request=self.req,
            name="Второй вход",
            operator="eq",
            expected=True,
            priority="desired",
        )
        self.observe(name="Второй вход", value=None, boolean=False, unit="")
        self.assertEqual(self.result()["status"], "pass")
        self.assertEqual(self.result()["rows"][1]["status"], "desired_fail")

    def test_blocker_priority_keeps_unknowns(self):
        self.observe(value=12)
        Requirement.objects.create(request=self.req, name="Вентиляция", operator="eq")
        r = self.result()
        self.assertEqual(r["status"], "blocker")
        self.assertEqual(r["rows"][1]["status"], "clarify")

    def test_unit_mismatch_requires_clarification(self):
        self.observe(unit="Вт")
        self.assertEqual(self.result()["status"], "clarify")

    def test_no_required_requirement_prevents_check(self):
        self.power.priority = "desired"
        self.power.save()
        with self.assertRaises(ValueError):
            self.result()
        self.assertEqual(Evaluation.objects.count(), 0)

    def test_economy_73000_3000_57000_170000(self):
        e = economics(self.p, self.req)
        self.assertEqual(Decimal(e["monthly_total"]), 73000)
        self.assertTrue(e["complete"])
        self.assertEqual(Decimal(e["gap"]), 3000)
        self.assertEqual(Decimal(e["allowed_rent"]), 57000)
        self.assertEqual(Decimal(e["once_total"]), 170000)
        self.assertEqual(Decimal(e["stress_total"]), 78000)
        self.observe()
        self.assertEqual(self.result()["status"], "pass")

    def test_incomplete_monthly_and_budget_warning(self):
        self.p.other_monthly = None
        self.p.save()
        e = economics(self.p, self.req)
        self.assertFalse(e["complete"])
        self.assertEqual(Decimal(e["monthly_total"]), 73000)
        self.assertIn("Превышение", e["budget_label"])
        self.assertIsNone(e["allowed_rent"])
        self.p.operating = None
        self.p.save()
        self.assertNotEqual(
            economics(self.p, self.req)["budget_label"], "В пределах месячного лимита"
        )

    def test_vat_and_rent_conflict(self):
        self.p.vat_mode = "excluded"
        self.p.vat_rate = Decimal("20")
        self.p.save()
        e = economics(self.p, self.req)
        self.assertEqual(Decimal(e["monthly_total"]), 85000)
        self.assertEqual(Decimal(e["allowed_rent"]), 47500)
        self.p.rent = 50000
        self.p.save()
        e = economics(self.p, self.req)
        self.assertTrue(e["rent_conflict"])
        self.assertFalse(e["complete"])

    def test_negative_remaining_budget(self):
        self.req.budget = 10000
        self.req.save()
        self.assertEqual(Decimal(economics(self.p, self.req)["allowed_rent"]), -3000)

    def test_immutable_report_and_staleness(self):
        self.observe()
        self.result()
        old = self.issue()
        data = bytes(old.pdf)
        snap = old.snapshot.copy()
        self.req.version += 1
        self.req.save()
        with self.assertRaises(ValueError):
            self.issue()
        evaluate(self.p, self.owner, self.req)
        new = self.issue()
        old.refresh_from_db()
        self.assertEqual(bytes(old.pdf), data)
        self.assertEqual(old.snapshot, snap)
        self.assertEqual(new.version, 2)
        self.assertEqual(
            self.client.get(f"/app/reports/{old.pk}/download").status_code, 200
        )

    def test_premises_edit_invalidates_only_its_check(self):
        self.observe()
        self.result()
        other = make_premises(request=self.req, label="Б")
        self.observe(p=other)
        evaluate(other, self.owner, self.req)
        self.p.version += 1
        self.p.save()
        self.assertIsNone(current_evaluation(self.p, self.req))
        self.assertIsNotNone(current_evaluation(other, self.req))

    def test_report_duplicate_click_and_duplicate_content(self):
        self.observe()
        self.result()
        key = uuid.uuid4()
        a = self.issue(key=key)
        b = self.issue(key=key)
        c = self.issue()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(a.pk, c.pk)
        self.assertEqual(Report.objects.count(), 1)

    def test_pdf_cyrillic_sources_costs_and_no_internal_or_files(self):
        self.observe()
        self.result()
        Attachment.objects.create(
            premises=self.p,
            filename="CONFIDENTIAL_SOURCE_FILE.pdf",
            content_type="application/pdf",
            data=b"ORIGINAL_FILE_CONTENT",
            sha256="a" * 64,
            author=self.owner,
        )
        r = self.issue(comment="Клиентский комментарий")
        text = "".join(p.extract_text() for p in PdfReader(BytesIO(bytes(r.pdf))).pages)
        for s in [
            "Условная кофейня",
            "Учебный объект А",
            "Условный акт",
            "Клиентский комментарий",
            "73 000",
            "170 000",
            "Превышение 3 000",
        ]:
            self.assertIn(s, text)
        for s in [
            "SECRET_INTERNAL_NOTE",
            "CONFIDENTIAL_SOURCE_FILE",
            "ORIGINAL_FILE_CONTENT",
        ]:
            self.assertNotIn(s, text)

    def test_blocker_report_is_explicitly_rejected(self):
        self.observe(value=12)
        self.result()
        r = self.issue()
        self.assertEqual(
            r.snapshot["objects"][0]["decision"], "Отсеян при текущих условиях"
        )

    def test_differences_keeps_equal_blockers_and_unknown(self):
        other = make_premises(request=self.req, label="Вариант Б")
        self.observe(value=12)
        self.observe(p=other, value=12)
        evaluate(self.p, self.owner, self.req)
        evaluate(other, self.owner, self.req)
        self.assertEqual(
            len(compare_rows([self.p, other], True, request_obj=self.req)), 1
        )
        self.p.observations.all().delete()
        other.observations.all().delete()
        evaluate(self.p, self.owner, self.req)
        evaluate(other, self.owner, self.req)
        self.assertEqual(
            len(compare_rows([self.p, other], True, request_obj=self.req)), 1
        )

    def test_realtor_and_agency_isolation_on_all_routes(self):
        self.observe()
        self.result()
        report = self.issue()
        f = Attachment.objects.create(
            premises=self.p,
            origin_request=self.req,
            filename="private.pdf",
            data=b"private",
            content_type="application/pdf",
            sha256="b" * 64,
            author=self.owner,
        )
        urls = [
            f"/app/requests/{self.req.pk}/{s}"
            for s in ["profile", "objects", "checks", "compare", "reports"]
        ] + [
            f"/app/requests/{self.req.pk}/objects/{self.p.pk}",
            f"/app/reports/{report.pk}/download",
            f"/app/files/{f.pk}/download",
        ]
        for user in [self.colleague, self.outsider]:
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(
                self.client.post(
                    f"/app/requests/{self.req.pk}/checks", {"version": 1}
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.post(
                    f"/app/requests/{self.req.pk}/objects/{self.p.pk}/observations", {}
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.post(
                    f"/app/requests/{self.req.pk}/state",
                    {"version": 1, "action": "archive"},
                ).status_code,
                404,
            )

    def test_manager_reads_but_cannot_modify_colleague_request(self):
        self.client.force_login(self.manager)
        for suffix in ["profile", "objects", "checks", "compare", "reports"]:
            self.assertEqual(
                self.client.get(
                    f"/app/requests/{self.req.pk}/{suffix}", follow=True
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    f"/app/requests/{self.req.pk}/{suffix}", {}
                ).status_code,
                405 if suffix == "objects" else 403,
            )
        self.assertEqual(
            self.client.post(
                f"/app/requests/{self.req.pk}/objects/{self.p.pk}", {}
            ).status_code,
            403,
        )

    def test_deactivated_member_loses_existing_session_and_files(self):
        self.observe()
        self.result()
        r = self.issue()
        m = self.owner.membership
        m.active = False
        m.save()
        for url in ["/app/requests", f"/app/reports/{r.pk}/download"]:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(f"/app/requests/{self.req.pk}/profile").status_code, 200
        )

    def test_team_only_manager_and_cannot_disable_last_manager(self):
        self.assertEqual(self.client.get("/app/team").status_code, 403)
        self.client.force_login(self.manager)
        self.client.post(
            "/app/team",
            {"action": "deactivate", "member_id": self.manager.membership.pk},
        )
        self.manager.membership.refresh_from_db()
        self.assertTrue(self.manager.membership.active)
        self.client.post(
            "/app/team",
            {"action": "deactivate", "member_id": self.outsider.membership.pk},
        )
        self.outsider.membership.refresh_from_db()
        self.assertTrue(self.outsider.membership.active)

    def test_invite_activation_password_and_single_use(self):
        self.client.force_login(self.manager)
        self.client.post(
            "/app/team",
            {
                "action": "invite",
                "email": "new@example.invalid",
                "creation_key": uuid.uuid4(),
            },
        )
        i = Invitation.objects.get(email="new@example.invalid")
        self.client.logout()
        self.assertEqual(self.client.get(f"/invite/{i.token}").status_code, 200)
        response = self.client.post(
            f"/invite/{i.token}",
            {
                "name": "Новый сотрудник",
                "new_password1": "New-strong-password-946!",
                "new_password2": "New-strong-password-946!",
            },
        )
        self.assertEqual(response.status_code, 302)
        u = User.objects.get(username=i.email)
        self.assertTrue(u.check_password("New-strong-password-946!"))
        self.assertEqual(u.membership.agency, self.agency)
        self.assertEqual(self.client.get(f"/invite/{i.token}").status_code, 410)

    def test_expired_invitation(self):
        i = Invitation.objects.create(
            agency=self.agency,
            email="expired@example.invalid",
            created_by=self.manager,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(self.client.get(f"/invite/{i.token}").status_code, 410)

    def test_pilot_idempotency_contact_validation(self):
        d = {
            "agency": "Тестовое",
            "city": "Новосибирск",
            "person": "Ирина",
            "contact": "test@example.invalid",
            "consent": "on",
            "creation_key": uuid.uuid4(),
        }
        for _ in range(2):
            self.assertContains(self.client.post("/pilot", d), "Заявка принята")
        d["creation_key"] = uuid.uuid4()
        self.client.post("/pilot", d)
        self.assertEqual(PilotApplication.objects.count(), 1)
        d["contact"] = "bad"
        self.assertContains(self.client.post("/pilot", d), "Укажите рабочий email")

    def test_optimistic_conflict_does_not_overwrite(self):
        d = {
            "title": "Изменённое",
            "city": "Новосибирск",
            "business": "Кофейня",
            "version": 0,
            "creation_key": self.req.creation_key,
            "requirements-TOTAL_FORMS": 0,
            "requirements-INITIAL_FORMS": 0,
        }
        response = self.client.post(
            f"/app/requests/{self.req.pk}/profile", grouped_payload(d, self.req)
        )
        self.assertEqual(response.status_code, 409)
        self.req.refresh_from_db()
        self.assertEqual(self.req.title, "Условная кофейня")

    def test_requirement_range_negative_and_duplicate_validation(self):
        self.assertFalse(
            RequirementForm(
                {
                    "name": "Площадь",
                    "unit": "м²",
                    "operator": "range",
                    "minimum": 50,
                    "maximum": 20,
                    "priority": "required",
                }
            ).is_valid()
        )
        self.assertFalse(
            RequirementForm(
                {
                    "name": "Мощность",
                    "operator": "gte",
                    "minimum": -1,
                    "priority": "required",
                }
            ).is_valid()
        )

    def test_files_validate_content_and_not_extension(self):
        bad = AttachmentForm(
            files={
                "file": SimpleUploadedFile(
                    "fake.pdf", b"not a pdf", content_type="application/pdf"
                )
            }
        )
        self.assertFalse(bad.is_valid())
        self.observe()
        self.result()
        pdf = bytes(self.issue().pdf)
        good = AttachmentForm(files={"file": SimpleUploadedFile("any.bin", pdf)})
        self.assertTrue(good.is_valid())

    def test_cross_request_selection_rejected(self):
        other = ClientRequest.objects.create(
            agency=self.agency, owner=self.owner, title="Другой запрос"
        )
        foreign = make_premises(request=other, label="Чужой вариант")
        response = self.client.get(
            f"/app/requests/{self.req.pk}/compare", {"objects": [self.p.pk, foreign.pk]}
        )
        self.assertContains(response, "Выберите корректный вариант")

    def test_completed_and_archive_require_explicit_reopen(self):
        self.client.post(
            f"/app/requests/{self.req.pk}/state", {"version": 1, "action": "done"}
        )
        self.req.refresh_from_db()
        self.assertEqual(self.req.stage, "done")
        self.assertEqual(
            self.client.post(
                f"/app/requests/{self.req.pk}/objects/{self.p.pk}", {}
            ).status_code,
            403,
        )
        self.client.post(
            f"/app/requests/{self.req.pk}/state",
            {"version": self.req.version, "action": "archive"},
        )
        self.assertNotContains(self.client.get("/app/requests"), "Условная кофейня")
        self.assertContains(
            self.client.get("/app/requests?stage=archive"), "Условная кофейня"
        )

    def test_csrf_protects_mutations(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.owner)
        self.assertEqual(
            c.post(f"/app/requests/{self.req.pk}/checks", {"version": 1}).status_code,
            403,
        )

    def test_post_login_returns_to_deep_link(self):
        self.client.logout()
        url = f"/app/requests/{self.req.pk}/objects"
        response = self.client.get(url)
        self.assertIn("next=", response.url)
        response = self.client.post(
            "/login",
            {
                "username": self.owner.username,
                "password": "Strong-test-password-942!",
                "next": url,
            },
        )
        self.assertRedirects(response, url)

    def test_archive_fixture_scope_and_dates(self):
        records = json.loads(
            (settings.BASE_DIR / "tests/data/archive_samples.json").read_text()
        )
        self.assertEqual(
            {r["scope"] for r in records}, {"premises", "building", "room", "reference"}
        )
        for row in records:
            self.assertTrue(row["date"])
            self.assertTrue(row["source_url"])

    def test_full_http_journey_without_crm(self):
        # A newly invited user proceeds through the same server forms as the browser.
        inv = Invitation.objects.create(
            agency=self.agency,
            email="journey@example.invalid",
            created_by=self.manager,
            expires_at=timezone.now() + timedelta(days=1),
        )
        c = Client()
        activation = c.post(
            f"/invite/{inv.token}",
            {
                "name": "Тестовый риелтор",
                "new_password1": "Sufficiently-Random-8jN6%",
                "new_password2": "Sufficiently-Random-8jN6%",
            },
        )
        self.assertEqual(activation.status_code, 302)
        request_key = uuid.uuid4()
        profile = {
            "title": "Кофейня · сквозной тест",
            "city": "Новосибирск",
            "business": "Кофейня",
            "budget": "70000",
            "version": 1,
            "creation_key": request_key,
            "requirements-TOTAL_FORMS": 1,
            "requirements-INITIAL_FORMS": 0,
            "requirements-0-name": "Мощность",
            "requirements-0-unit": "кВт",
            "requirements-0-operator": "gte",
            "requirements-0-minimum": "15",
            "requirements-0-expected": "on",
            "requirements-0-priority": "required",
        }
        profile = grouped_payload(
            profile,
            ClientRequest(
                owner=User.objects.get(username=inv.email), agency=self.agency
            ),
        )
        response = c.post("/app/requests/new", profile)
        self.assertEqual(response.status_code, 302)
        req = ClientRequest.objects.get(creation_key=request_key)
        c.post("/app/requests/new", profile)
        self.assertEqual(
            ClientRequest.objects.filter(creation_key=request_key).count(), 1
        )
        for label, power in [("Вариант А", 15), ("Вариант Б", 12)]:
            pkey = uuid.uuid4()
            data = {
                "label": label,
                "premises_type": "unknown",
                "area": "60",
                "rate": "1000",
                "operating": "8000",
                "utilities": "5000",
                "other_monthly": "0",
                "vat_mode": "included",
                "commercial_source_type": "manual",
                "commercial_status": "unverified",
                "deposit": "60000",
                "commission": "20000",
                "adaptation": "90000",
                "other_once": "0",
                "stress": "5000",
                "version": 1,
                "creation_key": pkey,
            }
            url = f"/app/requests/{req.pk}/objects/new"
            self.assertEqual(c.post(url, data).status_code, 302)
            c.post(url, data)
            self.assertEqual(Premises.objects.filter(creation_key=pkey).count(), 1)
            p = Premises.objects.get(creation_key=pkey)
            okey = uuid.uuid4()
            observation = {
                "characteristic": Characteristic.objects.get(code="power_kw").pk,
                "value": str(power),
                "unit": "кВт",
                "scope": "premises",
                "source_type": "document",
                "source": "Условный акт",
                "observed_on": str(timezone.localdate()),
                "status": "confirmed",
                "confirmation": "Условный подтверждающий документ",
                "version": 1,
                "creation_key": okey,
            }
            url = f"/app/requests/{req.pk}/objects/{p.pk}/observations"
            self.assertEqual(c.post(url, observation).status_code, 302)
            c.post(url, observation)
            self.assertEqual(Observation.objects.filter(creation_key=okey).count(), 1)
        self.assertEqual(
            c.post(f"/app/requests/{req.pk}/checks", {"version": 1}).status_code, 302
        )
        ids = list(req.premises.values_list("id", flat=True))
        self.assertContains(
            c.get(
                f"/app/requests/{req.pk}/compare", {"objects": ids, "differences": "1"}
            ),
            "Блокер",
        )
        report_form = {
            "objects": ids,
            "comment": "Учебный подбор",
            "creation_key": uuid.uuid4(),
            "action": "preview",
            "version": req.version,
            "selection_version": req.selection_version,
        }
        url = f"/app/requests/{req.pk}/reports"
        self.assertContains(c.post(url, report_form), "ПРЕДПРОСМОТР КЛИЕНТСКОЙ ВЕРСИИ")
        report_form["action"] = "issue"
        self.assertEqual(c.post(url, report_form).status_code, 302)
        c.post(url, report_form)
        self.assertEqual(req.reports.count(), 1)
        report = req.reports.get()
        response = c.get(f"/app/reports/{report.pk}/download")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))

    def test_next_without_profile_edits_keeps_evaluation_current(self):
        self.observe()
        self.result()
        d = {
            "title": self.req.title,
            "city": self.req.city,
            "business": self.req.business,
            "budget": "70000",
            "start_budget": "180000",
            "version": 1,
            "creation_key": self.req.creation_key,
            "requirements-TOTAL_FORMS": 1,
            "requirements-INITIAL_FORMS": 1,
            "requirements-0-id": self.power.pk,
            "requirements-0-name": "Мощность",
            "requirements-0-unit": "кВт",
            "requirements-0-operator": "gte",
            "requirements-0-minimum": "15.00",
            "requirements-0-expected": "on",
            "requirements-0-priority": "required",
        }
        self.assertRedirects(
            self.client.post(
                f"/app/requests/{self.req.pk}/profile", grouped_payload(d, self.req)
            ),
            f"/app/requests/{self.req.pk}/refine",
        )
        self.req.refresh_from_db()
        self.assertEqual(self.req.version, 1)
        self.assertIsNotNone(current_evaluation(self.p, self.req))

    def test_sidebar_contains_premises_and_no_workflow_tabs(self):
        response = self.client.get(f"/app/requests/{self.req.pk}/profile")
        self.assertContains(response, 'aria-label="Помещения"')
        self.assertNotContains(response, 'class="tabs"')
        self.assertNotContains(response, "05 Отчёты")
        self.assertContains(response, "Далее: уточнить профиль")

    def test_all_premises_respects_agency_and_owner(self):
        req = ClientRequest.objects.create(
            agency=self.agency, owner=self.colleague, title="Коллега"
        )
        make_premises(request=req, label="Вариант коллеги")
        foreign = ClientRequest.objects.create(
            agency=self.other_agency, owner=self.outsider, title="Другое агентство"
        )
        make_premises(request=foreign, label="Закрытый чужой вариант")
        response = self.client.get("/app/premises")
        self.assertContains(response, self.p.label)
        self.assertContains(response, "Вариант коллеги")
        self.client.force_login(self.manager)
        response = self.client.get("/app/premises")
        self.assertContains(response, "Вариант коллеги")
        self.assertNotContains(response, "Закрытый чужой вариант")
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/app/premises/new"), "Новое помещение")

    def test_refinement_only_asks_missing_conditions_and_saves(self):
        response = self.client.get(f"/app/requests/{self.req.pk}/refine")
        self.assertNotContains(response, 'id="id_power"')
        self.assertContains(response, 'id="id_area_min"')
        data = {
            "version": 1,
            "budget": "65000",
            "districts": ["Ленинский"],
            "area_min": "50",
            "area_max": "80",
            "area_priority": "required",
            "water": "yes",
            "water_priority": "required",
            "ventilation": "na",
            "ventilation_priority": "desired",
        }
        self.assertRedirects(
            self.client.post(f"/app/requests/{self.req.pk}/refine", data),
            f"/app/requests/{self.req.pk}/objects",
        )
        self.req.refresh_from_db()
        self.assertEqual(self.req.version, 2)
        self.assertEqual(self.req.budget, 65000)
        self.assertEqual(self.req.districts, ["Ленинский"])
        self.assertEqual(self.req.requirements.get(name="Площадь").operator, "range")
        self.assertEqual(
            self.req.requirements.get(name="Отдельная вытяжка").priority, "na"
        )
        count = self.req.requirements.count()
        self.assertEqual(
            self.client.post(f"/app/requests/{self.req.pk}/refine", data).status_code,
            409,
        )
        self.assertEqual(self.req.requirements.count(), count)

    def test_refinement_unknown_does_not_create_fake_conditions(self):
        self.client.post(
            f"/app/requests/{self.req.pk}/refine",
            {
                "version": 1,
                "budget": 70000,
                "area_priority": "desired",
                "water_priority": "desired",
                "ventilation_priority": "desired",
            },
        )
        self.assertEqual(self.req.requirements.count(), 1)
        self.assertFalse(self.req.requirements.filter(name="Вода").exists())

    def test_combined_page_read_only_manager_and_legacy_redirects(self):
        self.observe()
        self.result()
        for suffix in ["checks", "reports"]:
            response = self.client.get(f"/app/requests/{self.req.pk}/{suffix}")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/compare", response.url)
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.post(
                f"/app/requests/{self.req.pk}/compare",
                {"action": "show", "objects": [self.p.pk]},
            ).status_code,
            302,
        )
        for action in ["check", "parameter", "issue"]:
            self.assertEqual(
                self.client.post(
                    f"/app/requests/{self.req.pk}/compare",
                    {"action": action, "objects": [self.p.pk]},
                ).status_code,
                403,
            )

    def test_optional_parameters_persist_and_change_report_snapshot(self):
        desired = Requirement.objects.create(
            request=self.req, name="Второй вход", operator="eq", priority="desired"
        )
        self.observe()
        self.result()
        old = self.issue()
        original = bytes(old.pdf)
        url = f"/app/requests/{self.req.pk}/compare"
        data = {
            "version": 1,
            "selection_version": 1,
            "objects": [self.p.pk],
            "action": "parameter",
            "parameter": f"requirement:{desired.pk}",
            "comment": "Не терять комментарий",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.req.refresh_from_db()
        self.assertEqual(self.req.selection_version, 2)
        self.assertEqual(self.req.version, 1)
        self.assertIn(f"requirement:{desired.pk}", self.req.report_parameters)
        self.assertIsNotNone(current_evaluation(self.p, self.req))
        snapshot = report_snapshot(self.req, [self.p], "", parameters=[])
        self.assertEqual(len(snapshot["objects"][0]["rows"]), 1)
        snapshot = report_snapshot(
            self.req, [self.p], "", parameters=self.req.report_parameters
        )
        self.assertEqual(len(snapshot["objects"][0]["rows"]), 2)
        self.assertContains(self.client.get(url), "Не терять комментарий")
        # A stale toggle does not undo the newer composition.
        self.assertContains(self.client.post(url, data), "Состав отчёта изменён")
        old.refresh_from_db()
        self.assertEqual(bytes(old.pdf), original)
        data.update(selection_version=2, parameter="requirement:99999")
        self.assertContains(self.client.post(url, data), "Параметр недоступен")

    def test_pdf_required_risks_cannot_be_removed_and_landscape_layout(self):
        self.observe(value=12)
        self.result()
        snapshot = report_snapshot(self.req, [self.p], "", parameters=[])
        snapshot.update(
            version=1,
            issued_at=timezone.localtime().isoformat(),
            author="Тест",
            agency=self.agency.name,
        )
        from .pdf import render_pdf

        reader = PdfReader(BytesIO(render_pdf(snapshot)))
        self.assertGreater(
            reader.pages[0].mediabox.width, reader.pages[0].mediabox.height
        )
        text = "".join(p.extract_text() for p in reader.pages)
        for phrase in [
            "Блокер",
            "Отсеян при текущих условиях",
            "Превышение 3 000",
            "Условный акт",
            "Источники",
            "Допущения",
        ]:
            self.assertIn(phrase, text)
        self.assertNotIn("Ежемесячные платежи", text)
        self.assertNotIn("SECRET_INTERNAL_NOTE", text)

    def test_parser_only_managers_or_superuser_without_fetching(self):
        self.assertEqual(self.client.get("/app/parser").status_code, 403)
        self.assertNotContains(self.client.get("/app/premises"), "Парсинг объявлений")
        self.client.force_login(self.manager)
        self.assertContains(
            self.client.get("/app/parser"),
            "загрузка страниц и извлечение данных не выполняются",
        )
        self.assertContains(self.client.get("/app/premises"), "Парсинг объявлений")
        self.manager.membership.active = False
        self.manager.membership.save()
        self.assertEqual(self.client.get("/app/parser").status_code, 403)
        operator = User.objects.create_superuser(
            "operator", password="Operator-only-test-password-753!"
        )
        self.client.force_login(operator)
        self.assertEqual(self.client.get("/app/parser").status_code, 200)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, connections
from django.test import TransactionTestCase
from unittest import skipUnless


@skipUnless(
    connection.vendor == "postgresql", "Row-lock concurrency is verified on PostgreSQL"
)
class ConcurrentSubmissionTests(TransactionTestCase):
    def setUp(self):
        self.a = Agency.objects.create(name="Concurrency demo")
        self.u = User.objects.create_user(
            "concurrency@example.invalid", password="Strong-test-password-975!"
        )
        Membership.objects.create(user=self.u, agency=self.a)
        self.req = ClientRequest.objects.create(
            agency=self.a, owner=self.u, title="Concurrency demo"
        )
        Requirement.objects.create(
            request=self.req, name="Мощность", unit="кВт", minimum=15
        )
        self.p = make_premises(request=self.req, label="Условное помещение")
        Observation.objects.create(
            premises=self.p,
            name="Мощность",
            unit="кВт",
            value=15,
            status="confirmed",
            source_type="document",
            source="Демо",
            observed_on=timezone.localdate(),
            confirmation="Демо",
            author=self.u,
        )
        evaluate(self.p, self.u, self.req)

    def test_concurrent_report_submissions_are_single_version(self):
        barrier = Barrier(2)

        def worker(_):
            try:
                req = ClientRequest.objects.get(pk=self.req.pk)
                p = Premises.objects.get(pk=self.p.pk)
                u = User.objects.get(pk=self.u.pk)
                barrier.wait(timeout=10)
                return issue_report(req, [p], "", u, uuid.uuid4()).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(worker, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(Report.objects.count(), 1)

    def test_concurrent_pilot_submissions_are_single_record(self):
        barrier = Barrier(2)

        def worker(_):
            try:
                barrier.wait(timeout=10)
                return (
                    Client()
                    .post(
                        "/pilot",
                        {
                            "agency": "Демо",
                            "city": "Новосибирск",
                            "person": "Демо",
                            "contact": "demo@example.invalid",
                            "consent": "on",
                            "creation_key": uuid.uuid4(),
                        },
                    )
                    .status_code
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, range(2)))
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(PilotApplication.objects.count(), 1)
