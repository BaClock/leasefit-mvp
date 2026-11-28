from django import forms

from .geography import NOVOSIBIRSK, districts_for_city, place_key
from .listing_filters import CATEGORIES
from .metrapi import SOURCES
from .parser_schedule import MAXIMUM


class SearchFilters(forms.Form):
    locality = forms.CharField(
        label="Город", initial=NOVOSIBIRSK, max_length=120,
        widget=forms.TextInput(attrs={"list": "metrapi-cities"}),
    )
    districts = forms.MultipleChoiceField(
        label="Районы Новосибирска", choices=[(name, name) for name in districts_for_city(NOVOSIBIRSK)], required=False,
    )
    source = forms.MultipleChoiceField(label="Площадки", choices=SOURCES.items(), required=False)
    area_min = forms.DecimalField(label="Площадь от, м²", required=False, min_value=0, max_digits=12, decimal_places=2)
    area_max = forms.DecimalField(label="Площадь до, м²", required=False, min_value=0, max_digits=12, decimal_places=2)
    rent_min = forms.DecimalField(label="Аренда в месяц от, ₽", required=False, min_value=0, max_digits=14, decimal_places=2)
    rent_max = forms.DecimalField(label="Аренда в месяц до, ₽", required=False, min_value=0, max_digits=14, decimal_places=2)
    keywords = forms.CharField(label="Ключевые слова через запятую", required=False, max_length=400)
    exclude_words = forms.CharField(label="Исключающие слова через запятую", required=False, max_length=400)

    def clean(self):
        data = super().clean()
        if data.get("districts") and place_key(data.get("locality") or "") != place_key(NOVOSIBIRSK):
            self.add_error("districts", "Выберите районы только для Новосибирска.")
        for low, high in (("area_min", "area_max"), ("rent_min", "rent_max"),
                          ("rate_min", "rate_max"), ("floor_min", "floor_max"), ("date_from", "date_to")):
            if data.get(low) is not None and data.get(high) is not None and data[low] > data[high]:
                self.add_error(high, "Верхняя граница меньше нижней.")
        return data

    def saved_filters(self, *, exclude=()):
        return {
            name: value if isinstance(value, (list, int, bool)) else str(value)
            for name, value in self.cleaned_data.items()
            if name not in exclude and value not in (None, "", [])
        }


class ManualSearchForm(SearchFilters):
    query = forms.CharField(label="Улица, адрес или текст", required=False, max_length=250)
    categories = forms.MultipleChoiceField(label="Категории", choices=CATEGORIES, required=False)
    rate_min = forms.DecimalField(label="Ставка за м² в месяц от, ₽", required=False, min_value=0, max_digits=14, decimal_places=2)
    rate_max = forms.DecimalField(label="Ставка за м² в месяц до, ₽", required=False, min_value=0, max_digits=14, decimal_places=2)
    floor_min = forms.IntegerField(label="Этаж от", required=False, min_value=-20, max_value=200)
    floor_max = forms.IntegerField(label="Этаж до", required=False, min_value=-20, max_value=200)
    date_from = forms.DateField(label="Опубликовано с", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="Опубликовано по", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    availability = forms.ChoiceField(label="Актуальность", required=False, choices=[("active", "Действующие"), ("all", "Включая снятые")])
    maximum = forms.IntegerField(label="Максимум объявлений за поиск", required=False, min_value=1, max_value=100000)


class ScheduleForm(SearchFilters):
    enabled = forms.BooleanField(label="Включить категорию", required=False)
    interval_minutes = forms.IntegerField(label="Интервал, мин.", min_value=10, max_value=10080)
    offset_minutes = forms.IntegerField(label="Минута первого запуска", min_value=0, max_value=59)
    maximum = forms.IntegerField(label="Максимум объявлений за запуск", min_value=1, max_value=MAXIMUM)
    priority = forms.IntegerField(label="Приоритет при одинаковом времени (1 — первый)", min_value=1, max_value=100)
