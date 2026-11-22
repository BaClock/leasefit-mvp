from django import forms

from .business import FORMAT_CHOICES, QUESTION_GROUPS, QUESTIONS
from .operation_forms import structured_field
from .operations import ACCESS_QUESTIONS, normalize_value

ACCESS_ANSWERS = {"customers": "visitor_entry", "unloading": "unloading_methods"}
YES_NO = [("", "Пока неизвестно"), ("yes", "Да"), ("no", "Нет")]


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
                if key in ACCESS_ANSWERS:
                    continue
                self.fields[key] = forms.ChoiceField(
                    label=QUESTIONS[key],
                    required=False,
                    choices=YES_NO,
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
            "multi_select", "unloading_methods", requirement=True
        )
        self.fields["visitor_entry"].label = "Особый режим входа (если нужен)"
        self.fields["unloading_methods"].label = "Способ разгрузки (если важен)"
        self.fields["operating_schedule"].label = ACCESS_QUESTIONS["operating_schedule"]
        for answer, detail in ACCESS_ANSWERS.items():
            self.fields[answer] = forms.ChoiceField(
                label=ACCESS_QUESTIONS[detail], choices=YES_NO, required=False
            )
            if detail in self.initial and answer not in self.initial:
                self.initial[answer] = "yes"
        self.operating_questions = [
            self[key]
            for key in (
                "customers",
                "visitor_entry",
                "unloading",
                "unloading_methods",
                "operating_schedule",
            )
        ]

    def answers(self):
        category = self.cleaned_data["business_format"].split(".")[0]
        keys = (
            *QUESTION_GROUPS.get(category, ()),
            "operating_schedule",
        )
        if category == "food":
            keys += ("equipment_notes",)
        answers = {
            key: self.cleaned_data[key] for key in keys if self.cleaned_data.get(key)
        }
        for answer, detail in ACCESS_ANSWERS.items():
            value = self.cleaned_data[answer]
            if answer not in self.data and self.cleaned_data[detail]:
                value = "yes"
            if value:
                answers[answer] = value
            else:
                answers.pop(answer, None)
            if value == "yes" and self.cleaned_data[detail]:
                answers[detail] = self.cleaned_data[detail]
        if "unloading_methods" in answers:
            answers["unloading_methods"] = normalize_value(
                "multi_select", "unloading_methods", answers["unloading_methods"]
            )
        return answers
