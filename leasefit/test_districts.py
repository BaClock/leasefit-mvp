from django.test import TestCase
from django.urls import reverse

from .geography import ANY_DISTRICT, NOVOSIBIRSK, districts_for_city
from .management.commands.seed_novosibirsk import seed
from .models import ClientRequest, Premises
from .tests import grouped_payload


class DistrictPickerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, (cls.req, *_rest) = seed(include_links=False)
        cls.a, cls.b, cls.c = Premises.objects.filter(agency=cls.req.agency).order_by(
            "pk"
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.profile = reverse("profile", args=[self.req.pk])
        self.refine = reverse("refine", args=[self.req.pk])
        self.objects = reverse("objects", args=[self.req.pk])

    def test_create_profile_with_multiple_districts_and_reopen(self):
        url = reverse("request_new")
        page = self.client.get(url)
        obj = page.context["form"].instance
        self.assertContains(
            page, "В каких районах Новосибирска рассматриваете помещение?"
        )
        self.assertNotContains(page, 'name="geography"')
        self.assertEqual(len(page.context["form"].fields["districts"].choices), 11)
        payload = grouped_payload(
            {
                "version": obj.version,
                "creation_key": obj.creation_key,
                "city": NOVOSIBIRSK,
                "business": "Кофейня",
                "title": "Проверка районов",
                "districts": ["Октябрьский", "Ленинский"],
                "power_kw_priority": "required",
                "power_kw_min": "15",
            },
            obj,
        )
        response = self.client.post(url, payload)
        created = ClientRequest.objects.get(creation_key=obj.creation_key)
        self.assertRedirects(response, reverse("refine", args=[created.pk]))
        self.assertEqual(created.districts, ["Ленинский", "Октябрьский"])
        reopened = self.client.get(reverse("profile", args=[created.pk]))
        self.assertEqual(
            reopened.context["form"]["districts"].value(), created.districts
        )
        results = self.client.get(reverse("objects", args=[created.pk])).context[
            "recommendation_page"
        ]
        self.assertEqual({card["p"] for card in results}, {self.a, self.c})

    def test_change_districts_in_refinement_and_any_district(self):
        def save(choice):
            self.req.refresh_from_db()
            return self.client.post(
                self.refine,
                {
                    "version": self.req.version,
                    "budget": self.req.budget,
                    "districts": choice,
                    "area_priority": "desired",
                    "water_priority": "desired",
                    "ventilation_priority": "desired",
                },
            )

        self.assertRedirects(save(["Ленинский"]), self.objects)
        self.req.refresh_from_db()
        self.assertEqual(self.req.districts, ["Ленинский"])
        self.assertEqual(
            [
                card["p"]
                for card in self.client.get(self.objects).context["recommendation_page"]
            ],
            [self.a],
        )
        all_districts = list(districts_for_city(NOVOSIBIRSK))
        self.assertRedirects(save(all_districts), self.objects)
        self.req.refresh_from_db()
        self.assertEqual(self.req.districts, all_districts)
        self.assertEqual(
            self.client.get(self.refine).context["form"]["districts"].value(),
            all_districts,
        )
        self.assertRedirects(save([ANY_DISTRICT]), self.objects)
        self.req.refresh_from_db()
        self.assertEqual(self.req.districts, [])
        self.assertEqual(
            self.client.get(self.refine).context["form"]["districts"].value(),
            [ANY_DISTRICT],
        )
        self.assertEqual(
            len(self.client.get(self.objects).context["recommendation_page"]), 3
        )
        self.assertRedirects(save(["Советский"]), self.objects)
        self.assertContains(
            self.client.get(self.objects), "По текущим фильтрам вариантов нет."
        )
        self.assertContains(save(["Выдуманный"]), "Выберите корректный вариант")
        self.req.refresh_from_db()
        self.assertEqual(self.req.districts, ["Советский"])

    def test_legacy_location_remains_after_saving_picker(self):
        self.req.geography = "лЕнинский район; рядом с метро"
        self.req.save(update_fields=["geography"])
        page = self.client.get(self.profile)
        self.assertEqual(page.context["form"]["districts"].value(), ["Ленинский"])
        self.assertContains(page, self.req.geography)
        payload = grouped_payload(
            {
                "title": self.req.title,
                "city": self.req.city,
                "business": self.req.business,
                "version": self.req.version,
                "budget": self.req.budget,
                "start_budget": self.req.start_budget,
                "creation_key": self.req.creation_key,
                "districts": ["Октябрьский"],
            },
            self.req,
        )
        self.assertRedirects(self.client.post(self.profile, payload), self.refine)
        self.req.refresh_from_db()
        self.assertEqual(self.req.districts, ["Октябрьский"])
        self.assertEqual(self.req.geography, "лЕнинский район; рядом с метро")
        self.assertNotContains(self.client.get(self.objects), "рядом с метро")
        self.req.city = "Бердск"
        self.req.save(update_fields=["city"])
        self.assertFalse(
            self.client.get(self.profile)
            .context["form"]
            .fields["districts"]
            .widget.supported
        )

    def test_unknown_location_is_listed_separately_and_keeps_technical_rating(self):
        initial = next(
            card
            for card in self.client.get(self.objects).context["recommendation_page"]
            if card["p"] == self.a
        )
        self.req.districts = ["Ленинский"]
        self.req.save(update_fields=["districts"])
        self.a.location_confirmed = False
        self.a.save(update_fields=["location_confirmed"])
        self.b.district = ""
        self.b.save(update_fields=["district"])
        page = self.client.get(self.objects)
        groups = page.context["recommendation_groups"]
        self.assertEqual(groups["pass"], [])
        self.assertEqual({card["p"] for card in groups["location"]}, {self.a, self.b})
        actual = next(card for card in groups["location"] if card["p"] == self.a)
        self.assertEqual(actual["status"], initial["status"])
        self.assertEqual(actual["score"], initial["score"])
        self.assertContains(page, "Местоположение требует уточнения", count=2)
