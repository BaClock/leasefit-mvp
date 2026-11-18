from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import perf_counter
from unittest import skipUnless
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .management.commands.seed_novosibirsk import seed
from .models import (
    Agency,
    AuditEvent,
    Characteristic,
    ClientRequest,
    Evaluation,
    Membership,
    Observation,
    Premises,
    Report,
    RequestPremises,
    Requirement,
)
from .services import current_evaluation, evaluate, issue_report


def attach_data(req, obj):
    return {"version": req.version, "premises_version": obj.version}


def attach_url(req, obj):
    return reverse("attach_recommendation", args=[req.pk, obj.pk])


def page_cards(response):
    return list(response.context["recommendation_page"])


class RecommendationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, (cls.coffee, cls.cafe, cls.pickup) = seed(include_links=False)
        cls.agency = cls.coffee.agency
        cls.a, cls.b, cls.c = Premises.objects.filter(agency=cls.agency).order_by("pk")

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("objects", args=[self.coffee.pk])

    def test_blank_shortlist_evaluates_catalog_without_mutating_records(self):
        def state():
            return (
                list(
                    ClientRequest.objects.values_list(
                        "id", "version", "selection_version", "updated_at"
                    )
                ),
                list(Premises.objects.values_list("id", "version", "updated_at")),
                list(
                    Observation.objects.values_list(
                        "id", "value", "status", "structured_value"
                    )
                ),
                RequestPremises.objects.count(),
                Evaluation.objects.count(),
                AuditEvent.objects.count(),
                Report.objects.count(),
            )

        before = state()
        response = self.client.get(self.url)
        self.assertEqual(
            set(card["p"].pk for card in page_cards(response)),
            {self.a.pk, self.b.pk, self.c.pk},
        )
        self.assertEqual(response.context["recommendation_counts"]["pass"], 3)
        self.assertEqual(response.context["cards"], [])
        self.assertContains(response, "Выберите интересующие помещения из рекомендаций")
        self.assertContains(response, "Нормативные вопросы к проверке")
        self.assertEqual(before, state())

    def test_status_groups_keep_unknowns_ahead_of_confirmed_blockers(self):
        unknown = Premises.objects.create(agency=self.agency, label="Без подтверждений")
        char = Characteristic.objects.get(code="separate_exhaust")
        Requirement.objects.create(
            request=self.coffee,
            characteristic=char,
            name=char.label,
            operator="eq",
            priority="desired",
        )
        coffee_data = {
            card["p"].pk: card for card in page_cards(self.client.get(self.url))
        }
        self.assertEqual(coffee_data[self.a.pk]["status"], "pass")
        self.assertTrue(coffee_data[self.a.pk]["unmet_preferences"])
        response = self.client.get(
            reverse("objects", args=[self.cafe.pk]), {"sort": "score"}
        )
        results = {card["p"].pk: card for card in page_cards(response)}
        self.assertEqual(results[self.a.pk]["status"], "blocker")
        self.assertEqual(results[self.b.pk]["status"], "clarify")
        self.assertIsNone(results[unknown.pk]["score"])
        self.assertGreater(results[self.a.pk]["score"], 0)
        self.assertEqual(page_cards(response)[-1]["p"], self.a)
        self.assertContains(
            response, "Подтверждённых подходящих помещений пока не найдено"
        )
        self.assertContains(response, "Недостаточно данных для расчёта рейтинга")

    def test_confirmed_subtotal_and_unknown_finances_are_distinct(self):
        self.coffee.budget = 60000
        self.coffee.save(update_fields=["budget"])
        self.a.utilities = None
        self.a.save(update_fields=["utilities"])
        RequestPremises.objects.create(
            request=self.pickup,
            premises=self.a,
            stress=900000,
            adaptation=900000,
            internal_notes="PRIVATE_UNRELATED_COST_NOTES",
        )
        response = self.client.get(self.url, {"q": self.a.label})
        card = page_cards(response)[0]
        self.assertEqual(card["score"], 100)
        self.assertFalse(card["economics"]["complete"])
        self.assertEqual(card["economics"]["monthly_total"], "68000.00")
        self.assertIsNone(card["economics"]["stress_addition"])
        self.assertIsNone(card["economics"]["start_gap"])
        self.assertNotContains(response, "PRIVATE_UNRELATED_COST_NOTES")
        self.assertContains(response, "Полная сумма требует уточнения")
        self.assertNotContains(response, "Известные расходы выше лимита")
        self.a.commercial_status = "confirmed"
        self.a.save(update_fields=["commercial_status"])
        response = self.client.get(self.url, {"q": self.a.label})
        self.assertEqual(page_cards(response)[0]["score"], 60)
        self.assertContains(response, "Известные расходы выше лимита на 8 000 ₽")

    def test_selection_is_explicit_idempotent_and_keeps_catalog_objects(self):
        self.assertEqual(
            self.client.get(attach_url(self.coffee, self.a)).status_code, 405
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(
            csrf_client.post(
                attach_url(self.coffee, self.a), attach_data(self.coffee, self.a)
            ).status_code,
            403,
        )
        for obj in (self.a, self.a, self.b):
            self.assertRedirects(
                self.client.post(
                    attach_url(self.coffee, obj), attach_data(self.coffee, obj)
                ),
                self.url,
            )
        self.coffee.refresh_from_db()
        self.assertEqual(self.coffee.selection_version, 3)
        self.assertEqual(
            AuditEvent.objects.filter(action="request.premises.attached").count(), 2
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Уже в сравнении", count=2)
        self.assertEqual(
            {card["p"].pk for card in response.context["cards"]}, {self.a.pk, self.b.pk}
        )
        query = parse_qs(urlsplit(response.context["compare_url"]).query)
        self.assertEqual(set(query["objects"]), {str(self.a.pk), str(self.b.pk)})
        self.assertFalse(Evaluation.objects.exists())
        self.assertFalse(Report.objects.exists())
        url = reverse("detach_premises", args=[self.coffee.pk, self.a.pk])
        self.assertRedirects(self.client.post(url), self.url)
        self.assertTrue(Premises.objects.filter(pk=self.a.pk).exists())
        self.assertEqual(self.coffee.premises.get(), self.b)

    def test_attach_enforces_agency_owner_working_stage_and_versions(self):
        foreign_agency = Agency.objects.create(name="Стороннее агентство")
        foreign_obj = Premises.objects.create(
            agency=foreign_agency, label="ВНЕШНИЙ ОБЪЕКТ"
        )
        self.assertNotContains(self.client.get(self.url), foreign_obj.label)
        self.assertEqual(
            self.client.post(
                attach_url(self.coffee, foreign_obj),
                attach_data(self.coffee, foreign_obj),
            ).status_code,
            404,
        )
        response = self.client.post(
            attach_url(self.coffee, self.a),
            {"version": 0, "premises_version": self.a.version},
            follow=True,
        )
        self.assertContains(response, "Профиль или помещение изменились")
        manager = User.objects.create_user("recommendation-manager")
        Membership.objects.create(user=manager, agency=self.agency, role="manager")
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertNotContains(self.client.get(self.url), "Добавить к сравнению")
        self.assertEqual(
            self.client.post(
                attach_url(self.coffee, self.a), attach_data(self.coffee, self.a)
            ).status_code,
            403,
        )
        self.client.force_login(self.user)
        self.coffee.archived = True
        self.coffee.save(update_fields=["archived"])
        self.assertEqual(
            self.client.post(
                attach_url(self.coffee, self.a), attach_data(self.coffee, self.a)
            ).status_code,
            403,
        )
        self.assertFalse(RequestPremises.objects.exists())

    def test_geography_and_filters_use_structured_data(self):
        foreign = Premises.objects.create(
            agency=self.agency,
            label="Другой город",
            city="Бердск",
            location_confirmed=True,
        )
        provisional = Premises.objects.create(
            agency=self.agency, label="Адрес ещё не подтверждён", city="Бердск"
        )
        self.assertNotContains(self.client.get(self.url), foreign.label)
        self.assertContains(self.client.get(self.url), provisional.label)
        self.assertContains(
            self.client.get(self.url), "Местоположение требует уточнения"
        )
        self.a.premises_type = "food"
        self.a.save(update_fields=["premises_type"])
        filters = {
            "q": "Ленинский",
            "district": "Ленинский",
            "premises_type": "food",
            "area_min": 50,
            "area_max": 65,
            "monthly_max": 75000,
            "power_min": 15,
            "water": "yes",
            "only_confirmed": "on",
            "status": "pass",
        }
        self.assertEqual(
            [card["p"] for card in page_cards(self.client.get(self.url, filters))],
            [self.a],
        )
        self.assertFalse(
            page_cards(self.client.get(self.url, {**filters, "power_min": 16}))
        )
        self.client.post(
            attach_url(self.coffee, self.a), attach_data(self.coffee, self.a)
        )
        self.assertFalse(
            page_cards(self.client.get(self.url, {**filters, "only_new": "on"}))
        )
        self.assertEqual(
            [
                card["p"]
                for card in page_cards(
                    self.client.get(self.url, {"sort": "monthly", "status": "pass"})
                )
            ],
            [self.a, self.c, self.b],
        )
        self.c.area = 80
        self.c.save(update_fields=["area"])
        self.assertEqual(
            page_cards(self.client.get(self.url, {"sort": "area"}))[0]["p"], self.c
        )

    def test_profile_and_premises_updates_recalculate_independently_and_keep_pdf(self):
        routes = [
            reverse("objects", args=[req.pk])
            for req in (self.coffee, self.cafe, self.pickup)
        ]

        def status(url):
            return next(
                card["status"]
                for card in page_cards(self.client.get(url))
                if card["p"] == self.a
            )

        self.assertEqual(
            [status(url) for url in routes], ["pass", "blocker", "clarify"]
        )
        self.client.post(
            attach_url(self.coffee, self.a), attach_data(self.coffee, self.a)
        )
        ev = evaluate(self.a, self.user, self.coffee)
        report = issue_report(self.coffee, [self.a], "", self.user, uuid4())
        original = bytes(report.pdf)
        req = self.coffee.requirements.get(characteristic__code="power_kw")
        req.minimum = 25
        req.save(update_fields=["minimum"])
        self.coffee.version += 1
        self.coffee.save(update_fields=["version"])
        self.assertEqual(status(self.url), "blocker")
        self.assertIsNone(current_evaluation(self.a, self.coffee))
        Observation.objects.filter(
            premises=self.a, characteristic__code="power_kw"
        ).update(value=30)
        self.a.version += 1
        self.a.save(update_fields=["version"])
        self.assertEqual(status(self.url), "pass")
        self.assertEqual(
            evaluate(self.a, self.user, self.coffee).result["status"], "pass"
        )
        self.assertTrue(Evaluation.objects.filter(pk=ev.pk).exists())
        report.refresh_from_db()
        self.assertEqual(bytes(report.pdf), original)

    def test_empty_catalog_and_missing_mandatory_requirements(self):
        blank = ClientRequest.objects.create(
            agency=self.agency, owner=self.user, title="Без требований"
        )
        response = self.client.get(reverse("objects", args=[blank.pk]))
        self.assertContains(response, "Укажите хотя бы одно обязательное требование")
        self.assertFalse(page_cards(response))
        user = User.objects.create_user("empty-recommendation-catalog")
        agency = Agency.objects.create(name="Пустой каталог")
        Membership.objects.create(agency=agency, user=user)
        req = ClientRequest.objects.create(
            agency=agency, owner=user, title="Пустой каталог"
        )
        self.client.force_login(user)
        self.assertContains(
            self.client.get(reverse("objects", args=[req.pk])),
            "В каталоге пока нет помещений",
        )

    def test_pagination_and_query_count_for_500_candidates(self):
        power = Characteristic.objects.get(code="power_kw")
        objects = Premises.objects.bulk_create(
            [
                Premises(
                    agency=self.agency,
                    label=f"Нагрузочный вариант {i:03}",
                    area=500 if i == 499 else 20 + i % 50,
                    rent=50000,
                    operating=0,
                    utilities=0,
                    other_monthly=0,
                    vat_mode="included",
                )
                for i in range(500)
            ]
        )
        Observation.objects.bulk_create(
            [
                Observation(
                    premises=obj,
                    characteristic=power,
                    name=power.label,
                    value=15,
                    unit="кВт",
                    status="confirmed",
                    confirmation="Вымышленный источник",
                    author=self.user,
                    source_type="owner",
                    observed_on=timezone.localdate(),
                )
                for obj in objects
            ]
        )
        filters = {"sort": "area", "power_min": 15, "q": "Нагрузочный вариант"}
        start = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            first = self.client.get(self.url, filters)
        elapsed = perf_counter() - start
        captured = queries.captured_queries
        self.assertGreater(len(captured), 0)
        self.assertLessEqual(
            len(captured), 16, [entry["sql"][:200] for entry in captured]
        )
        self.assertEqual(first.context["recommendation_counts"]["pass"], 500)
        self.assertEqual(first.context["recommendation_page"].paginator.num_pages, 25)
        self.assertEqual(len(page_cards(first)), 20)
        self.assertEqual(page_cards(first)[0]["p"], objects[-1])
        later = self.client.get(self.url, {**filters, "page": 25})
        self.assertEqual(len(page_cards(later)), 20)
        self.assertNotIn(objects[-1], [card["p"] for card in page_cards(later)])
        print(
            f"Recommendation benchmark: 500 candidates, {len(captured)} SQL queries, {elapsed:.3f} s"
        )


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class ConcurrentRecommendationTests(TransactionTestCase):
    def test_two_posts_create_one_link_and_one_audit_event(self):
        user = User.objects.create_user("concurrent-recommendation-owner")
        agency = Agency.objects.create(name="Тест конкурентного добавления")
        Membership.objects.create(agency=agency, user=user)
        req = ClientRequest.objects.create(agency=agency, owner=user, title="Запрос")
        obj = Premises.objects.create(agency=agency, label="Общее помещение")
        barrier = Barrier(2)

        def submit():
            try:
                client = Client()
                client.force_login(User.objects.get(pk=user.pk))
                barrier.wait(timeout=10)
                return client.post(
                    attach_url(req, obj), attach_data(req, obj)
                ).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(results, [302, 302])
        self.assertEqual(
            RequestPremises.objects.filter(request=req, premises=obj).count(), 1
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="request.premises.attached").count(), 1
        )
        req.refresh_from_db()
        self.assertEqual(req.selection_version, 2)
