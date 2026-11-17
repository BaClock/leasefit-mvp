import re, uuid
from io import BytesIO
from PIL import Image
from django import forms
from django.forms import inlineformset_factory, BaseInlineFormSet
from django.contrib.auth.forms import SetPasswordForm
from .models import *


class VersionForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput, initial=1)
    creation_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["version"].initial = self.instance.version
            self.fields["creation_key"].initial = self.instance.creation_key


class RequestForm(VersionForm):
    class Meta:
        model = ClientRequest
        fields = ["title", "city", "business", "geography", "budget", "start_budget"]


class RequirementForm(forms.ModelForm):
    class Meta:
        model = Requirement
        fields = [
            "name",
            "unit",
            "operator",
            "minimum",
            "maximum",
            "expected",
            "priority",
        ]

    def clean(self):
        d = super().clean()
        if not d.get("name"):
            return d
        if d.get("operator") != "eq" and d.get("minimum") is None:
            self.add_error("minimum", "Укажите порог.")
        if d.get("operator") == "range":
            if d.get("maximum") is None:
                self.add_error("maximum", "Укажите верхнюю границу.")
            elif d.get("minimum") is not None and d["minimum"] > d["maximum"]:
                self.add_error("maximum", "Верхняя граница меньше нижней.")
        return d


class RequirementsBase(BaseInlineFormSet):
    def clean(self):
        super().clean()
        names = []
        for f in self.forms:
            d = getattr(f, "cleaned_data", {})
            if not d or d.get("DELETE") or not d.get("name"):
                continue
            key = d["name"].strip().casefold()
            if key in names:
                raise forms.ValidationError(
                    "Один параметр указан дважды. Объедините пороги в диапазон; противоречивые требования не сохраняются."
                )
            names.append(key)


RequirementsFormSet = inlineformset_factory(
    ClientRequest,
    Requirement,
    form=RequirementForm,
    formset=RequirementsBase,
    extra=3,
    can_delete=True,
)


class PremisesForm(VersionForm):
    class Meta:
        model = Premises
        exclude = ["request", "updated_at", "version", "creation_key"]
        widgets = {
            "internal_notes": forms.Textarea(attrs={"rows": 3}),
            "assumptions": forms.Textarea(attrs={"rows": 3}),
            "commercial_date": forms.DateInput(
                attrs={"type": "date"}, format="%Y-%m-%d"
            ),
        }

    def clean(self):
        d = super().clean()
        if d.get("commercial_date") and d["commercial_date"] > timezone.localdate():
            self.add_error("commercial_date", "Дата не может быть в будущем.")
        if d.get("commercial_status") == "confirmed" and (
            not d.get("commercial_source")
            or not d.get("commercial_date")
            or not d.get("commercial_confirmation")
            or d.get("commercial_source_type") == "listing"
        ):
            self.add_error(
                "commercial_status",
                "Подтверждение требует источника, даты и основания. Объявление само по себе не подтверждает условия.",
            )
        return d

    def clean_vat_rate(self):
        v = self.cleaned_data.get("vat_rate")
        if v is not None and v > 100:
            raise forms.ValidationError("Укажите ставку от 0 до 100%.")
        return v


class ObservationForm(forms.ModelForm):
    creation_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    class Meta:
        model = Observation
        exclude = ["premises", "superseded", "author", "created_at", "creation_key"]
        widgets = {
            "observed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
        }

    def clean(self):
        d = super().clean()
        if d.get("observed_on") and d["observed_on"] > timezone.localdate():
            self.add_error("observed_on", "Дата не может быть в будущем.")
        if d.get("status") == "confirmed":
            if (
                d.get("value") is None
                and d.get("boolean") is None
                and not d.get("text")
            ):
                self.add_error("status", "Подтверждение требует значения.")
            if not d.get("confirmation"):
                self.add_error("confirmation", "Укажите основание подтверждения.")
            if d.get("source_type") == "listing":
                self.add_error(
                    "source_type",
                    "Объявление само по себе не подтверждает параметр. Добавьте ответ собственника или документ.",
                )
        return d


class AttachmentForm(forms.Form):
    file = forms.FileField(label="PDF, JPG или PNG, до 20 МБ")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > 20 * 1024 * 1024:
            raise forms.ValidationError("Размер больше 20 МБ.")
        data = f.read()
        f.seek(0)
        if data.startswith(b"%PDF-"):
            # Validate PDF structure, not the filename or submitted MIME type.
            try:
                from pypdf import PdfReader

                reader = PdfReader(BytesIO(data), strict=True)
                if reader.is_encrypted:
                    raise ValueError()
                len(reader.pages)
            except Exception:
                raise forms.ValidationError(
                    "Не удалось прочитать PDF. Зашифрованные файлы не принимаются."
                )
            f.verified_type = "application/pdf"
        else:
            try:
                image = Image.open(BytesIO(data))
                image.verify()
                if image.format not in ["JPEG", "PNG"]:
                    raise ValueError()
                f.verified_type = {"JPEG": "image/jpeg", "PNG": "image/png"}[
                    image.format
                ]
            except Exception:
                raise forms.ValidationError("Разрешены читаемые PDF, JPG и PNG.")
        return f


class PilotForm(forms.ModelForm):
    creation_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    consent = forms.BooleanField(
        label="Согласен на обработку данных для рассмотрения заявки (см. условия ниже)"
    )

    class Meta:
        model = PilotApplication
        fields = ["agency", "city", "person", "contact", "types", "comment", "consent"]

    def clean_contact(self):
        v = self.cleaned_data["contact"].strip()
        if "@" in v:
            forms.EmailField().clean(v)
        elif (
            not re.fullmatch(r"[+\d\s()\-]+", v)
            or not 10 <= len(re.sub(r"\D", "", v)) <= 15
        ):
            raise forms.ValidationError(
                "Укажите рабочий email или телефон (10–15 цифр)."
            )
        return v


class InviteForm(forms.Form):
    email = forms.EmailField(label="Рабочий email сотрудника")
    creation_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class ActivateForm(SetPasswordForm):
    name = forms.CharField(label="Ваше имя", max_length=150)


class SelectionForm(forms.Form):
    objects = forms.ModelMultipleChoiceField(
        label="Выберите варианты",
        queryset=Premises.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )
    comment = forms.CharField(
        label="Клиентский комментарий",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    creation_key = forms.UUIDField(
        widget=forms.HiddenInput, initial=uuid.uuid4, required=False
    )

    def __init__(self, *args, request_obj, minimum=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.minimum = minimum
        self.fields["objects"].queryset = request_obj.premises.all().order_by("id")

    def clean_objects(self):
        q = self.cleaned_data["objects"]
        if not self.minimum <= len(q) <= 4:
            raise forms.ValidationError(
                f"Выберите от {self.minimum} до 4 вариантов одного запроса."
            )
        return q


class RefinementForm(forms.Form):
    version = forms.IntegerField(widget=forms.HiddenInput)
    budget = forms.DecimalField(
        label="Какой предел полной нагрузки в месяц, ₽?",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    geography = forms.CharField(
        label="Какие районы или ориентиры подходят?", required=False, max_length=250
    )
    area_min = forms.DecimalField(
        label="Площадь от, м²",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    area_max = forms.DecimalField(
        label="Площадь до, м²",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    area_priority = forms.ChoiceField(
        label="Насколько важна площадь?",
        choices=Requirement.PRIORITIES[:2],
        initial="desired",
    )
    power = forms.DecimalField(
        label="Какая минимальная выделенная мощность нужна, кВт?",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=2,
    )
    power_priority = forms.ChoiceField(
        label="Насколько важна мощность?",
        choices=Requirement.PRIORITIES[:2],
        initial="desired",
    )
    water = forms.ChoiceField(
        label="Нужно подключение воды в помещении?",
        required=False,
        choices=[
            ("", "Пока неизвестно"),
            ("yes", "Да, требуется"),
            ("na", "Не требуется для этого бизнеса"),
        ],
    )
    water_priority = forms.ChoiceField(
        label="Насколько важна вода?",
        choices=Requirement.PRIORITIES[:2],
        initial="desired",
    )
    ventilation = forms.ChoiceField(
        label="Нужна отдельная вытяжка?",
        required=False,
        choices=[
            ("", "Пока неизвестно"),
            ("yes", "Да, требуется"),
            ("na", "Не требуется для этого бизнеса"),
        ],
    )
    ventilation_priority = forms.ChoiceField(
        label="Насколько важна вытяжка?",
        choices=Requirement.PRIORITIES[:2],
        initial="desired",
    )

    def __init__(self, *args, request_obj, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_obj = request_obj
        self.initial.update(
            version=request_obj.version,
            budget=request_obj.budget,
            geography=request_obj.geography,
        )
        names = " ".join(
            request_obj.requirements.values_list("name", flat=True)
        ).casefold()
        self.groups = []
        for key, words, fields in [
            ("area", ["площад", "area"], ["area_min", "area_max", "area_priority"]),
            ("power", ["мощност", "электр", "power"], ["power", "power_priority"]),
            ("water", ["вод", "water"], ["water", "water_priority"]),
            (
                "ventilation",
                ["вытяж", "вентиля", "ventilation"],
                ["ventilation", "ventilation_priority"],
            ),
        ]:
            if any(word in names for word in words):
                for field in fields:
                    self.fields.pop(field)
            else:
                self.groups.append(key)

    def clean(self):
        d = super().clean()
        if (
            d.get("area_min") is not None
            and d.get("area_max") is not None
            and d["area_min"] > d["area_max"]
        ):
            self.add_error("area_max", "Верхняя граница меньше нижней.")
        return d
