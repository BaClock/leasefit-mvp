from django import forms

from .business import FORMAT_CHOICES, QUESTION_GROUPS, QUESTIONS
from .operation_forms import structured_field
from .operations import normalize_value


class BusinessSetupForm(forms.Form):
    business_format = forms.ChoiceField(
        label="Категория и подкатегория",
        choices=[("", "Выберите подкатегорию"), *FORMAT_CHOICES],
    )

    def __init__(self, *args, request_obj, **kwargs):
        super().__init__(
            *args,
            initial={
                "business_format": request_obj.business_format,
                **request_obj.business_answers,
            },
            **kwargs
        )
        self.questions = {}
        for category, keys in QUESTION_GROUPS.items():
            self.questions[category] = []
            for key in keys:
                self.fields[key] = forms.ChoiceField(
                    label=QUESTIONS[key],
                    required=False,
                    choices=[("", "Пока неизвестно"), ("yes", "Да"), ("no", "Нет")],
                )
                self.questions[category].append(self[key])
        self.fields["equipment_notes"] = forms.CharField(
            label="Какое оборудование предусмотрено?", max_length=500, required=False
        )
        self.questions["food"].append(self["equipment_notes"])
        self.fields["visitor_entry"] = structured_field("enum", "visitor_entry")
        self.fields["operating_schedule"] = structured_field(
            "time_schedule", "operating_schedule"
        )
        self.fields["unloading_methods"] = structured_field(
            "multi_select", "unloading_methods"
        )
        self.fields["unloading_methods"].label = "Нужные способы разгрузки"
        self.operating_questions = [
            self[key]
            for key in ("visitor_entry", "operating_schedule", "unloading_methods")
        ]

    def answers(self):
        category = self.cleaned_data["business_format"].split(".")[0]
        keys = (
            *QUESTION_GROUPS.get(category, ()),
            "visitor_entry",
            "operating_schedule",
            "unloading_methods",
        )
        if category == "food":
            keys += ("equipment_notes",)
        answers = {
            key: self.cleaned_data[key] for key in keys if self.cleaned_data.get(key)
        }
        if "unloading_methods" in answers:
            answers["unloading_methods"] = normalize_value(
                "multi_select", "unloading_methods", answers["unloading_methods"]
            )
        return answers
