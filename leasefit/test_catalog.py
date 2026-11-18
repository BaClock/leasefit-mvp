import hashlib
import json
import uuid
from decimal import Decimal
from io import BytesIO
from django.test import Client, TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader
from .models import (
    Agency,
    Membership,
    ClientRequest,
    Premises,
    RequestPremises,
    Characteristic,
    Requirement,
    Observation,
    Evaluation,
    Attachment,
    AuditEvent,
    Report,
)
from .forms import GroupedRequirementsForm, CustomRequirementsFormSet, ObservationForm
from .services import (
    evaluate,
    current_evaluation,
    issue_report,
    report_snapshot,
    economics,
    compare_rows,
)


class SharedCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.agency = Agency.objects.create(name="Общее агентство")
        cls.other = Agency.objects.create(name="Другое агентство")
        cls.owner = User.objects.create_user(
            "owner", password="Long-owner-password-2026!"
        )
        cls.peer = User.objects.create_user("peer", password="Long-peer-password-2026!")
        cls.outsider = User.objects.create_user(
            "outsider", password="Long-outsider-password-2026!"
        )
        for u, a in [
            (cls.owner, cls.agency),
            (cls.peer, cls.agency),
            (cls.outsider, cls.other),
        ]:
            Membership.objects.create(user=u, agency=a)
        cls.coffee = ClientRequest.objects.create(
            agency=cls.agency, owner=cls.owner, title="Кофейня", budget=70000
        )
        cls.pickup = ClientRequest.objects.create(
            agency=cls.agency, owner=cls.peer, title="ПВЗ", budget=90000
        )
        cls.p = Premises.objects.create(
            agency=cls.agency,
            label="Общая карточка №105",
            area=95,
            rent=60000,
            operating=8000,
            utilities=5000,
            other_monthly=0,
            vat_mode="included",
        )
        RequestPremises.objects.create(
            request=cls.coffee,
            premises=cls.p,
            internal_notes="PRIVATE COFFEE NOTE",
            stress=5000,
            adaptation=90000,
            other_once=0,
        )
        RequestPremises.objects.create(
            request=cls.pickup,
            premises=cls.p,
            internal_notes="PRIVATE PICKUP NOTE",
            stress=0,
            adaptation=0,
            other_once=0,
        )
        power = Characteristic.objects.get(code="power_kw")
        Requirement.objects.create(
            request=cls.coffee,
            characteristic=power,
            name="Для кофе электричество",
            unit="кВт",
            minimum=25,
        )
        Requirement.objects.create(
            request=cls.pickup,
            characteristic=power,
            name="Для ПВЗ мощность",
            unit="кВт",
            minimum=15,
        )
        cls.o = Observation.objects.create(
            premises=cls.p,
            characteristic=power,
            name="Другой текст названия",
            unit="кВт",
            value=15,
            source_type="document",
            source="Акт №105",
            observed_on=timezone.localdate(),
            status="confirmed",
            confirmation="Акт на конкретное помещение",
            author=cls.owner,
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def test_delete_premises_checks_permissions_and_preserves_issued_pdf(self):
        url = reverse("delete_premises", args=[self.p.pk])
        self.assertContains(self.client.get(url), "руководитель", status_code=403)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.peer)
        self.peer.membership.role = "manager"
        self.peer.membership.save(update_fields=["role"])

        evaluate(self.p, self.owner, self.coffee)
        report = issue_report(self.coffee, [self.p], "", self.owner, uuid.uuid4())
        source = Attachment.objects.create(
            premises=self.p,
            filename="fact.pdf",
            data=b"file",
            content_type="application/pdf",
            sha256="file",
            author=self.owner,
        )
        self.o.document = source
        self.o.save(update_fields=["document"])

        confirmation = self.client.get(url)
        self.assertContains(confirmation, self.p.label)
        self.assertTrue(Premises.objects.filter(pk=self.p.pk).exists())
        data = {
            key: confirmation.context[key] for key in ("version", "selection_version")
        }
        self.assertRedirects(self.client.post(url, data), reverse("all_premises"))
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertFalse(Premises.objects.filter(pk=self.p.pk).exists())
        self.assertFalse(RequestPremises.objects.filter(premises_id=self.p.pk).exists())
        self.assertFalse(Evaluation.objects.filter(premises_id=self.p.pk).exists())
        self.assertFalse(Attachment.objects.filter(pk=source.pk).exists())
        self.assertEqual(bytes(Report.objects.get(pk=report.pk).pdf), bytes(report.pdf))
        for req in [self.coffee, self.pickup]:
            req.refresh_from_db()
            self.assertEqual(req.selection_version, 2)
        self.assertTrue(
            AuditEvent.objects.filter(action="premises.deleted", actor=self.peer).exists()
        )

    def test_delete_unlinked_premises_rejects_stale_confirmation_and_requires_csrf(self):
        p = Premises.objects.create(agency=self.agency, label="Свободная карточка")
        url = reverse("delete_premises", args=[p.pk])
        self.assertContains(self.client.get(reverse("all_premises")), url)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        confirmation = client.get(url)
        data = {
            key: confirmation.context[key] for key in ("version", "selection_version")
        }
        self.assertEqual(client.post(url, data).status_code, 403)
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        RequestPremises.objects.create(request=self.coffee, premises=p)
        self.assertEqual(client.post(url, data).status_code, 409)
        self.assertTrue(Premises.objects.filter(pk=p.pk).exists())
        data["selection_version"] = client.get(url).context["selection_version"]
        self.assertRedirects(client.post(url, data), reverse("all_premises"))

    def test_delete_request_keeps_shared_premises_and_colleague_request(self):
        url = reverse("delete_request", args=[self.coffee.pk])
        self.peer.membership.role = "manager"
        self.peer.membership.save(update_fields=["role"])
        self.client.force_login(self.peer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("requests")), url)
        self.assertContains(self.client.get(reverse("profile", args=[self.coffee.pk])), url)

        evaluate(self.p, self.owner, self.coffee)
        evaluate(self.p, self.peer, self.pickup)
        report = issue_report(self.coffee, [self.p], "", self.owner, uuid.uuid4())
        source = Attachment.objects.create(
            premises=self.p,
            origin_request=self.coffee,
            filename="private.pdf",
            data=b"file",
            content_type="application/pdf",
            sha256="file",
            author=self.owner,
        )
        self.o.document = source
        self.o.save(update_fields=["document"])
        self.coffee.archived = True
        self.coffee.save(update_fields=["archived"])

        confirmation = self.client.get(url)
        self.assertContains(confirmation, self.coffee.title)
        self.assertTrue(ClientRequest.objects.filter(pk=self.coffee.pk).exists())
        data = {
            key: confirmation.context[key] for key in ("version", "selection_version")
        }
        self.assertRedirects(self.client.post(url, data), reverse("requests"))
        self.assertFalse(ClientRequest.objects.filter(pk=self.coffee.pk).exists())
        self.assertFalse(Report.objects.filter(pk=report.pk).exists())
        self.assertFalse(Attachment.objects.filter(pk=source.pk).exists())
        self.assertEqual(list(self.pickup.premises.all()), [self.p])
        self.p.refresh_from_db()
        self.assertEqual(self.p.evaluations.get().request_id, self.pickup.pk)
        self.assertIsNone(current_evaluation(self.p, self.pickup))
        self.o.refresh_from_db()
        self.assertIsNone(self.o.document_id)
        self.assertTrue(
            AuditEvent.objects.filter(action="request.deleted", actor=self.owner).exists()
        )

    def test_independent_creation_and_catalog_filters(self):
        data = {
            "label": "Новая карточка без запроса",
            "premises_type": "office",
            "area": 45,
            "rent": 30000,
            "vat_mode": "included",
            "commercial_source_type": "manual",
            "commercial_status": "unverified",
            "version": 1,
            "creation_key": uuid.uuid4(),
        }
        response = self.client.post("/app/premises/new", data)
        self.assertEqual(response.status_code, 302)
        p = Premises.objects.get(label=data["label"])
        self.assertEqual(p.agency, self.agency)
        self.assertFalse(p.request_links.exists())
        self.assertContains(
            self.client.get("/app/premises?area_min=90&power_min=15"), self.p.label
        )
        self.assertNotContains(
            self.client.get("/app/premises?power_min=25"), self.p.label
        )
        self.assertContains(
            self.client.get("/app/premises?premises_type=office&rent_max=35000"),
            p.label,
        )
        self.assertNotContains(
            self.client.get("/app/premises?area_min=90&area_max=20"), p.label
        )

    def test_reuse_unique_link_and_cross_agency_guard(self):
        req = ClientRequest.objects.create(
            agency=self.agency, owner=self.owner, title="Магазин"
        )
        url = f"/app/requests/{req.pk}/objects/select"
        count = Premises.objects.count()
        for _ in range(2):
            self.assertEqual(
                self.client.post(url, {"objects": [self.p.pk]}).status_code, 302
            )
        self.assertEqual(
            RequestPremises.objects.filter(request=req, premises=self.p).count(), 1
        )
        self.assertEqual(Premises.objects.count(), count)
        foreign = Premises.objects.create(
            agency=self.other, label="SECRET OTHER AGENCY"
        )
        self.assertNotContains(self.client.get("/app/premises"), foreign.label)
        self.assertEqual(
            self.client.get(f"/app/premises/{foreign.pk}").status_code, 404
        )
        self.assertEqual(
            self.client.post(url, {"objects": [foreign.pk]}).status_code, 200
        )
        self.assertFalse(req.premises.filter(pk=foreign.pk).exists())

    def test_same_property_different_profiles_and_labels(self):
        a = evaluate(self.p, self.owner, self.coffee)
        b = evaluate(self.p, self.peer, self.pickup)
        self.assertEqual(a.result["status"], "blocker")
        self.assertEqual(b.result["status"], "pass")
        self.assertEqual(a.request, self.coffee)
        self.assertEqual(b.request, self.pickup)
        self.assertEqual(Decimal(a.result["economics"]["gap"]), 3000)
        self.assertEqual(Decimal(b.result["economics"]["gap"]), -17000)
        self.assertEqual(Decimal(a.result["economics"]["stress_addition"]), 5000)
        self.assertEqual(Decimal(b.result["economics"]["stress_addition"]), 0)

    def test_shared_observation_requires_warning_and_invalidates_both(self):
        a = evaluate(self.p, self.owner, self.coffee)
        b = evaluate(self.p, self.peer, self.pickup)
        response = self.client.get(f"/app/premises/{self.p.pk}")
        self.assertContains(response, "Помещение используется в нескольких запросах")
        data = {
            "characteristic": self.o.characteristic_id,
            "value": 30,
            "scope": "premises",
            "source_type": "document",
            "source": "Новый акт",
            "observed_on": str(timezone.localdate()),
            "status": "confirmed",
            "confirmation": "Новый документ",
            "version": 1,
            "creation_key": uuid.uuid4(),
        }
        url = f"/app/premises/{self.p.pk}/observations"
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.p.refresh_from_db()
        self.assertEqual(self.p.version, 1)
        data["confirm_shared"] = "on"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.p.refresh_from_db()
        self.assertIsNone(current_evaluation(self.p, self.coffee))
        self.assertIsNone(current_evaluation(self.p, self.pickup))
        self.assertEqual(Evaluation.objects.filter(pk__in=[a.pk, b.pk]).count(), 2)

    def test_unlink_retains_history_pdfs_and_new_link_needs_recalculation(self):
        evaluate(self.p, self.owner, self.coffee)
        report = issue_report(
            self.coffee, [self.p], "Для клиента", self.owner, uuid.uuid4()
        )
        digest = hashlib.sha256(bytes(report.pdf)).hexdigest()
        old_link = RequestPremises.objects.get(request=self.coffee, premises=self.p).pk
        self.assertEqual(
            self.client.post(
                f"/app/requests/{self.coffee.pk}/objects/{self.p.pk}/detach"
            ).status_code,
            302,
        )
        self.assertTrue(Premises.objects.filter(pk=self.p.pk).exists())
        self.assertTrue(Observation.objects.filter(pk=self.o.pk).exists())
        self.assertTrue(
            Evaluation.objects.filter(request=self.coffee, premises=self.p).exists()
        )
        self.assertEqual(
            self.client.get(f"/app/reports/{report.pk}/download").status_code, 200
        )
        report.refresh_from_db()
        self.assertEqual(hashlib.sha256(bytes(report.pdf)).hexdigest(), digest)
        link = RequestPremises.objects.create(request=self.coffee, premises=self.p)
        self.assertNotEqual(link.pk, old_link)
        self.assertIsNone(current_evaluation(self.p, self.coffee))

    def test_shared_card_never_exposes_another_request_notes_or_private_files(self):
        private = Attachment.objects.create(
            premises=self.p,
            origin_request=self.coffee,
            filename="PRIVATE CLIENT FILE.pdf",
            data=b"private",
            content_type="application/pdf",
            sha256="a" * 64,
            author=self.owner,
        )
        self.client.force_login(self.peer)
        response = self.client.get(f"/app/premises/{self.p.pk}")
        for text in [
            "PRIVATE COFFEE NOTE",
            "PRIVATE PICKUP NOTE",
            "PRIVATE CLIENT FILE.pdf",
            self.coffee.title,
        ]:
            self.assertNotContains(response, text)
        self.assertEqual(
            self.client.get(f"/app/files/{private.pk}/download").status_code, 404
        )
        response = self.client.get(
            f"/app/requests/{self.pickup.pk}/objects/{self.p.pk}"
        )
        self.assertContains(response, "PRIVATE PICKUP NOTE")
        self.assertNotContains(response, "PRIVATE COFFEE NOTE")
        self.assertEqual(
            self.client.get(
                f"/app/requests/{self.coffee.pk}/objects/{self.p.pk}"
            ).status_code,
            404,
        )

    def test_grouped_requirements_absence_unknown_and_validation(self):
        req = ClientRequest.objects.create(
            agency=self.agency, owner=self.owner, title="Без воды"
        )

        def payload():
            f = GroupedRequirementsForm(request_obj=req)
            return {
                k: f[k].value() if f[k].value() is not None else "" for k in f.fields
            }

        data = payload()
        data.update(
            water_supply_priority="required",
            water_supply_expected="no",
            area_sqm_priority="desired",
            area_sqm_min=100,
            area_sqm_max=40,
        )
        form = GroupedRequirementsForm(data, request_obj=req)
        self.assertFalse(form.is_valid())
        self.assertIn("area_sqm_max", form.errors)
        data["area_sqm_max"] = 120
        form = GroupedRequirementsForm(data, request_obj=req)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(req)
        water = req.requirements.get(characteristic__code="water_supply")
        self.assertFalse(water.expected)
        RequestPremises.objects.create(request=req, premises=self.p)
        self.assertEqual(evaluate(self.p, self.owner, req).result["status"], "clarify")
        Observation.objects.create(
            premises=self.p,
            characteristic=water.characteristic,
            name="Вода",
            boolean=False,
            source_type="owner",
            source="Ответ",
            observed_on=timezone.localdate(),
            status="confirmed",
            confirmation="Ответ по этому помещению",
            author=self.owner,
        )
        self.assertEqual(evaluate(self.p, self.owner, req).result["status"], "pass")

    def test_template_adds_no_normative_or_overwrites(self):
        req = self.coffee
        old = req.requirements.get()
        old_min = old.minimum
        url = f"/app/requests/{req.pk}/template"
        self.client.post(url, {"version": 1, "business": "Кофейня"})
        self.assertEqual(req.requirements.count(), 1)
        self.client.post(
            url, {"version": 1, "business": "Кофейня", "confirm_template": "on"}
        )
        old.refresh_from_db()
        self.assertEqual(old.minimum, old_min)
        self.assertTrue(
            req.requirements.filter(
                characteristic__code="water_supply", priority="desired"
            ).exists()
        )
        self.assertFalse(
            req.requirements.exclude(pk=old.pk).filter(priority="required").exists()
        )

    def test_custom_requirement_stable_after_rename_and_observation_validation(self):
        from .characteristics import resolve_characteristic

        char = resolve_characteristic(self.agency.pk, "Особая нагрузка", "кг", "number")
        r = Requirement.objects.create(
            request=self.coffee,
            characteristic=char,
            name="Другое имя",
            unit="кг",
            minimum=50,
        )
        Observation.objects.create(
            premises=self.p,
            characteristic=char,
            name="Факт ещё с другим именем",
            unit="кг",
            value=60,
            source_type="document",
            source="Акт",
            observed_on=timezone.localdate(),
            status="confirmed",
            confirmation="Проверено",
            author=self.owner,
        )
        r.name = "Переименовано"
        r.save()
        result = evaluate(self.p, self.owner, self.coffee).result
        self.assertEqual(
            next(row for row in result["rows"] if row["characteristic_id"] == char.pk)[
                "status"
            ],
            "pass",
        )
        foreign = resolve_characteristic(
            self.other.pk, "Чужой параметр", "кг", "number"
        )
        form = ObservationForm(
            {"characteristic": foreign.pk}, agency=self.agency, premises=self.p
        )
        self.assertFalse(form.is_valid())
        self.assertIn("characteristic", form.errors)

    def test_custom_requirement_create_edit_and_repeat_through_profile(self):
        req = self.coffee
        url = f"/app/requests/{req.pk}/profile"

        def payload():
            response = self.client.get(url)
            result = {}
            for form in [response.context["form"], response.context["grouped"]]:
                for field in form:
                    result[field.html_name] = (
                        field.value() if field.value() is not None else ""
                    )
            return result

        data = payload()
        data.update(
            {
                "custom-TOTAL_FORMS": 1,
                "custom-INITIAL_FORMS": 0,
                "custom-0-name": "Допустимая нагрузка на перекрытие",
                "custom-0-kind": "number",
                "custom-0-unit": "кг/м²",
                "custom-0-operator": "gte",
                "custom-0-minimum": 300,
                "custom-0-priority": "required",
            }
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        custom = req.requirements.get(characteristic__agency=self.agency)
        original_identity = (custom.pk, custom.characteristic_id)
        data = payload()
        data.update(
            {
                "custom-TOTAL_FORMS": 1,
                "custom-INITIAL_FORMS": 1,
                "custom-0-id": custom.pk,
                "custom-0-name": "Нагрузка для оборудования клиента",
                "custom-0-kind": "number",
                "custom-0-unit": "кг/м²",
                "custom-0-operator": "gte",
                "custom-0-minimum": 450,
                "custom-0-priority": "desired",
            }
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        custom.refresh_from_db()
        self.assertEqual((custom.pk, custom.characteristic_id), original_identity)
        self.assertEqual(custom.minimum, 450)
        self.assertEqual(custom.name, "Нагрузка для оборудования клиента")
        self.assertEqual(custom.priority, "desired")
        self.assertEqual(
            req.requirements.filter(characteristic__agency=self.agency).count(), 1
        )
        # A repeated stale edit must neither overwrite nor duplicate the rule.
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertEqual(
            req.requirements.filter(characteristic__agency=self.agency).count(), 1
        )

    def test_distinct_rules_for_same_characteristic_and_exact_number(self):
        # Legacy profiles may have separate lower/upper conditions for one code.
        upper = Requirement.objects.create(
            request=self.coffee,
            characteristic=Characteristic.objects.get(code="power_kw"),
            name="Верхний предел мощности",
            unit="кВт",
            operator="lte",
            minimum=30,
        )
        evaluate(self.p, self.owner, self.coffee)
        rows = compare_rows([self.p], request_obj=self.coffee)
        by_id = {row["requirement_id"]: row["cells"][0]["status"] for row in rows}
        self.assertEqual(by_id[upper.pk], "pass")
        self.assertEqual(
            by_id[self.coffee.requirements.exclude(pk=upper.pk).get().pk], "blocker"
        )
        upper.operator = "eq"
        upper.minimum = Decimal("15")
        upper.save()
        self.assertIn("15 кВт", str(upper))
        self.assertNotIn("да", str(upper))


class CatalogMigrationTests(TransactionTestCase):
    """Exercise the actual historical 0004 schema, including binary payloads."""

    migrate_from = [("leasefit", "0004_clientrequest_report_parameters_and_more")]
    migrate_to = [("leasefit", "0008_operating_characteristics")]

    def test_legacy_ids_values_history_and_files_survive(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old = executor.loader.project_state(self.migrate_from).apps
        try:
            U = old.get_model("auth", "User")
            A = old.get_model("leasefit", "Agency")
            R = old.get_model("leasefit", "ClientRequest")
            P = old.get_model("leasefit", "Premises")
            user = U.objects.create(username="migration-owner")
            agency = A.objects.create(name="Migration agency")
            req = R.objects.create(
                agency=agency, owner=user, title="Исторический запрос"
            )
            p = P.objects.create(
                request=req,
                label="Сохранить ID",
                stress=5000,
                adaptation=90000,
                other_once=0,
                assumptions="PRIVATE ASSUMPTIONS",
                internal_notes="PRIVATE NOTES",
                version=7,
            )
            q = old.get_model("leasefit", "Requirement").objects.create(
                request=req, name="Мощность", unit="кВт", minimum=15
            )
            custom = old.get_model("leasefit", "Requirement").objects.create(
                request=req, name="Особая площадь", unit="m2", minimum=20
            )
            o = old.get_model("leasefit", "Observation").objects.create(
                premises=p,
                name="Электрическая мощность",
                unit="кВт",
                value=12,
                source="Архив",
                source_type="archive",
                observed_on="2012-12-07",
                author=user,
                confirmation="История",
            )
            ev = old.get_model("leasefit", "Evaluation").objects.create(
                premises=p,
                profile_version=1,
                premises_version=7,
                author=user,
                result={"old": "must remain"},
                rules_version="p0-1.0",
            )
            file = old.get_model("leasefit", "Attachment").objects.create(
                premises=p,
                filename="source.pdf",
                data=b"original-source",
                sha256="f" * 64,
                content_type="application/pdf",
                author=user,
            )
            report = old.get_model("leasefit", "Report").objects.create(
                request=req,
                version=1,
                snapshot={"old": "snapshot"},
                pdf=b"%PDF-original-do-not-change",
                fingerprint="e" * 64,
                author=user,
            )
            ids = (req.pk, p.pk, q.pk, o.pk, ev.pk, file.pk, report.pk)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_to)
        req_id, p_id, q_id, o_id, e_id, f_id, r_id = ids
        p = Premises.objects.get(pk=p_id)
        self.assertEqual(p.agency_id, agency.pk)
        self.assertEqual(p.version, 7)
        link = RequestPremises.objects.get(request_id=req_id, premises=p)
        self.assertEqual(link.internal_notes, "PRIVATE NOTES")
        self.assertEqual(link.assumptions, "PRIVATE ASSUMPTIONS")
        self.assertEqual(link.adaptation, 90000)
        self.assertEqual(link.stress, 5000)
        q = Requirement.objects.get(pk=q_id)
        o = Observation.objects.get(pk=o_id)
        self.assertEqual(q.characteristic_id, o.characteristic_id)
        self.assertEqual(q.characteristic.code, "power_kw")
        migrated_custom = Requirement.objects.get(pk=custom.pk)
        self.assertEqual(migrated_custom.unit, "м²")
        self.assertEqual(migrated_custom.characteristic.unit, "м²")
        self.assertEqual(migrated_custom.characteristic.agency_id, agency.pk)
        self.assertEqual(o.value, 12)
        self.assertEqual(str(o.observed_on), "2012-12-07")
        e = Evaluation.objects.get(pk=e_id)
        self.assertEqual(e.request_id, req_id)
        self.assertEqual(e.result, {"old": "must remain"})
        self.assertEqual(e.link_identity, link.pk)
        self.assertEqual(
            bytes(Attachment.objects.get(pk=f_id).data), b"original-source"
        )
        self.assertEqual(Attachment.objects.get(pk=f_id).origin_request_id, req_id)
        self.assertEqual(
            bytes(Report.objects.get(pk=r_id).pdf), b"%PDF-original-do-not-change"
        )
        self.assertEqual(Report.objects.get(pk=r_id).snapshot, {"old": "snapshot"})
        counts = (
            Premises.objects.count(),
            RequestPremises.objects.count(),
            Observation.objects.count(),
            Evaluation.objects.count(),
            Report.objects.count(),
        )
        MigrationExecutor(connection).migrate(self.migrate_to)
        self.assertEqual(
            counts,
            (
                Premises.objects.count(),
                RequestPremises.objects.count(),
                Observation.objects.count(),
                Evaluation.objects.count(),
                Report.objects.count(),
            ),
        )
