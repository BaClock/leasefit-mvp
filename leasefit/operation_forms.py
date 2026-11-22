from django import forms
from django.core.exceptions import ValidationError

from .operations import ACCESS_CHOICES, DAYS, normalize_value


class WeeklyScheduleWidget(forms.MultiWidget):
    template_name = "leasefit/schedule_widget.html"

    def __init__(self):
        time = {
            "placeholder": "ЧЧ:ММ",
            "size": 5,
            "maxlength": 5,
            "autocomplete": "off",
        }
        super().__init__(
            [
                forms.Select(
                    choices=[
                        ("", "Не задано"),
                        ("weekly", "По дням и часам"),
                        ("always", "Круглосуточно"),
                    ],
                    attrs={"data-schedule-mode": ""},
                ),
                *[forms.TextInput(attrs=time) for _ in range(14)],
            ],
            attrs={"data-schedule": ""},
        )

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widgets = context["widget"]["subwidgets"]
        context["schedule_days"] = list(zip(DAYS, widgets[1::2], widgets[2::2]))
        return context

    def decompress(self, value):
        if not isinstance(value, dict):
            return [""] * 15
        result = [value["mode"], *([""] * 14)]
        for day, start, end in value.get("intervals", []):
            result[1 + 2 * day] = f"{start // 60:02d}:{start % 60:02d}"
            result[2 + 2 * day] = f"{end // 60:02d}:{end % 60:02d}"
        return result


class WeeklyScheduleField(forms.Field):
    widget = WeeklyScheduleWidget

    def to_python(self, value):
        if not value or not value[0]:
            return None
        mode = value[0]
        if mode == "always":
            return {"mode": "always"}
        intervals = []
        for day, (start, end) in enumerate(zip(value[1::2], value[2::2])):
            if not start and not end:
                continue
            intervals.append([day, self.minutes(start), self.minutes(end)])
        if not intervals:
            raise ValidationError("Укажите часы хотя бы для одного дня.")
        return normalize_value(
            "time_schedule", "", {"mode": mode, "intervals": intervals}
        )

    @staticmethod
    def minutes(value):
        parts = (value or "").strip().split(":")
        if len(parts) != 2 or not all(
            piece.isascii() and piece.isdecimal() and len(piece) <= 2 for piece in parts
        ):
            raise ValidationError(
                "Для каждого выбранного дня заполните начало и конец в формате ЧЧ:ММ."
            )
        hour, minute = map(int, parts)
        if not 0 <= minute < 60 or not (0 <= hour < 24 or (hour, minute) == (24, 0)):
            raise ValidationError(
                "Укажите время от 00:00 до 24:00. Используйте 24:00 только как конец дня."
            )
        return hour * 60 + minute


class OperationOptions(forms.CheckboxSelectMultiple):
    def value_from_datadict(self, data, files, name):
        values = super().value_from_datadict(data, files, name)
        if isinstance(values, str):
            values = [values]
        return [item for item in (values or []) if item != ""]


def structured_field(kind, code, *, required=False, initial=None, requirement=False):
    if kind == "time_schedule":
        return WeeklyScheduleField(
            label="График",
            required=required,
            initial=initial,
        )
    choices = list(ACCESS_CHOICES[code].items())
    if requirement and code == "unloading_methods":
        choices = [(key, label) for key, label in choices if key != "none"]
    if kind == "enum":
        return forms.ChoiceField(
            label="Режим доступа",
            required=required,
            initial=initial,
            choices=[("", "Не задано"), *choices],
        )
    return forms.MultipleChoiceField(
        label="Выберите подходящие варианты",
        required=required,
        initial=initial or [],
        choices=choices,
        widget=OperationOptions,
    )
