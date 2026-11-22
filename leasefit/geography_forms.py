from django import forms

from .geography import (
    ANY_DISTRICT,
    NOVOSIBIRSK,
    districts_for_city,
    requested_districts,
)


class DistrictPicker(forms.CheckboxSelectMultiple):
    template_name = "leasefit/district_picker.html"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value or [ANY_DISTRICT], attrs)
        context["widget"].update(
            city=self.city,
            supported=self.supported,
            legacy=self.legacy,
            question=self.question,
        )
        return context


class DistrictSelectionField(forms.MultipleChoiceField):
    widget = DistrictPicker

    def __init__(self):
        super().__init__(
            label="В каких районах Новосибирска рассматриваете помещение?",
            required=False,
        )

    def configure(self, form, req, city):
        displayed_city = city if districts_for_city(city) else NOVOSIBIRSK
        self.choices = [
            (ANY_DISTRICT, "Любой район"),
            *((name, name) for name in districts_for_city(displayed_city)),
        ]
        self.widget.city = displayed_city
        self.widget.question = self.label
        self.widget.supported = bool(districts_for_city(city))
        self.widget.legacy = req.geography if req.districts is None else ""
        names, _ = requested_districts(req)
        form.initial["districts"] = names or [ANY_DISTRICT]

    def clean_selection(self, form, req, city):
        submitted = "districts-present" in form.data or "districts" in form.data
        if not (districts_for_city(city) and submitted):
            return req.districts
        values = form.cleaned_data["districts"]
        if ANY_DISTRICT in values:
            return []
        return [name for name in districts_for_city(city) if name in values]
