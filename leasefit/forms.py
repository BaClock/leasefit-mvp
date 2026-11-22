import re, uuid
from io import BytesIO
from PIL import Image
from django import forms
from .operations import (
    ACCESS_QUESTIONS,
    CUSTOM_KINDS,
    GENERAL_ACCESS,
    LEGACY_BY_REPLACEMENT,
    STRUCTURED,
    normalize_value,
)
from .operation_forms import structured_field
from django.db.models import Q
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
    geography = forms.CharField(
        label="В каких районах искать помещение?",
        help_text="Например, Ленинский, Кировский. Перечислите районы через запятую.",
        max_length=250,
        required=False,
    )
    business = forms.CharField(
        label="Формат бизнеса",
        max_length=160,
        widget=forms.TextInput(attrs={"list": "business-formats"}),
    )

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
    location_fields = {"city", "district", "location_confirmed"}
    essential_fields = {
        "label",
        "premises_type",
        "floor",
        "number",
        "listing_url",
        "area",
        *location_fields,
    }

    class Meta:
        model = Premises
        exclude = ["agency", "updated_at", "version", "creation_key"]
        widgets = {
            "internal_notes": forms.Textarea(attrs={"rows": 3}),
            "assumptions": forms.Textarea(attrs={"rows": 3}),
            "commercial_date": forms.DateInput(
                attrs={"type": "date"}, format="%Y-%m-%d"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound and not self.location_fields.intersection(self.data):
            # Older clients can edit the other fields without clearing geography.
            for field in self.location_fields:
                self.fields[field].disabled = True

    def clean(self):
        d = super().clean()
        if d.get("location_confirmed") and not d.get("city"):
            self.add_error("city", "Укажите город перед подтверждением местоположения.")
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


class CharacteristicSelect(forms.Select):
    def create_option(self, *args, **kwargs):
        option = super().create_option(*args, **kwargs)
        value = option.get("value")
        if hasattr(value, "instance"):
            option["attrs"].update(
                {
                    "data-kind": value.instance.value_type,
                    "data-unit": value.instance.unit,
                    "data-code": value.instance.code,
                }
            )
        return option


class ObservationForm(forms.ModelForm):
    creation_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    class Meta:
        model = Observation
        fields = [
            "characteristic",
            "value",
            "boolean",
            "text",
            "status",
            "confirmation",
            "scope",
            "locator",
            "source_type",
            "source",
            "source_url",
            "observed_on",
            "document",
        ]
        widgets = {
            "characteristic": CharacteristicSelect(),
            "observed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {
            "characteristic": "Характеристика",
            "value": "Число (единица фиксирована справочником)",
            "boolean": "Значение характеристики (да / нет)",
            "text": "Текстовое значение",
            "status": "Статус сведений",
            "confirmation": "Основание подтверждения или выбора источника",
        }
        help_texts = {
            "status": "Если значение неизвестно, выберите «Неизвестно». Для статуса «Подтверждено» укажите основание.",
            "confirmation": "Например, номер документа или дата письменного ответа собственника.",
        }

    def __init__(self, *args, agency=None, premises=None, visible_files=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["characteristic"].queryset = (
            Characteristic.objects.filter(
                Q(agency__isnull=True) | Q(agency=agency)
            ).order_by("group", "label")
            if agency
            else Characteristic.objects.none()
        )
        self.fields["document"].queryset = (
            visible_files if visible_files is not None else Attachment.objects.none()
        )
        self.fields["document"].label = "Подтверждающий документ из доступных вложений"
        self.fields["boolean"].widget.choices = [
            ("", "Выберите значение"),
            ("true", "Да"),
            ("false", "Нет"),
        ]
        from .characteristics import STANDARD

        structured_names = []
        for spec in STANDARD:
            if spec["value_type"] in STRUCTURED:
                code = spec["code"]
                field_name = "observed_" + code
                self.fields[field_name] = structured_field(spec["value_type"], code)
                structured_names.append(field_name)
        self.order_fields(
            ["characteristic", "value", "boolean", "text", *structured_names]
        )

    def clean(self):
        d = super().clean()
        char = d.get("characteristic")
        status = d.get("status")
        if d.get("observed_on") and d["observed_on"] > timezone.localdate():
            self.add_error("observed_on", "Дата не может быть в будущем.")
        if char:
            value_field = (
                "observed_" + char.code
                if char.value_type in STRUCTURED
                else {"number": "value", "boolean": "boolean", "text": "text"}[
                    char.value_type
                ]
            )
            val = d.get(value_field)
            if char.value_type in STRUCTURED:
                d["structured_value"] = (
                    normalize_value(char.value_type, char.code, val) if val else None
                )
            if char.value_type != "number" and d.get("value") is not None:
                self.add_error(
                    "value",
                    "Для этой характеристики числовое значение не используется.",
                )
            if char.value_type != "boolean" and d.get("boolean") is not None:
                self.add_error(
                    "boolean", "Для этой характеристики укажите число или текст."
                )
            if char.value_type != "text" and d.get("text"):
                self.add_error(
                    "text",
                    "Используйте поле числа или наличия для этой характеристики.",
                )
            for key in tuple(d):
                if (
                    key.startswith("observed_")
                    and key != "observed_on"
                    and key != value_field
                    and d[key]
                ):
                    self.add_error(
                        key, "Выберите значение только для указанной характеристики."
                    )
            if (
                status
                and status != "unknown"
                and value_field in d
                and val in (None, "", [])
            ):
                self.add_error(
                    value_field,
                    (
                        "Выберите «Да» или «Нет»."
                        if value_field == "boolean"
                        else "Введите значение характеристики."
                    ),
                )
            if status == "unknown" and val not in (None, "", []):
                self.add_error(
                    "status",
                    "Для известного значения выберите «Не подтверждено» или подтверждение.",
                )
        if status == "confirmed":
            if not d.get("confirmation"):
                self.add_error("confirmation", "Укажите основание подтверждения.")
            if d.get("source_type") == "listing":
                self.add_error(
                    "source_type",
                    "Объявление само по себе не подтверждает характеристику.",
                )
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.name = obj.characteristic.label
        obj.unit = obj.characteristic.unit
        obj.structured_value = self.cleaned_data.get("structured_value")
        if commit:
            obj.save()
        return obj


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
        label="В каких районах искать помещение?",
        help_text="Например, Ленинский, Кировский. Перечислите районы через запятую.",
        required=False,
        max_length=250,
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
        codes = set(
            request_obj.requirements.values_list("characteristic__code", flat=True)
        )
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
            if {
                "area": "area_sqm",
                "power": "power_kw",
                "water": "water_supply",
                "ventilation": "separate_exhaust",
            }[key] in codes:
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


from django.db.models import Q
from .characteristics import (
    STANDARD,
    GROUPS,
    LEGACY_ACCESS,
    ALIASES,
    normalized,
    resolve_characteristic,
)

IMPORTANCE = [
    ("na", "Не важно"),
    ("required", "Обязательно"),
    ("desired", "Желательно"),
]
FLOORS = [
    ("", "Не важно / не указано"),
    ("basement", "Подвал"),
    ("ground", "Цоколь"),
    ("1", "Первый этаж"),
    ("2", "Второй этаж"),
    ("3+", "Третий и выше"),
]


class GroupedRequirementsForm(forms.Form):
    def __init__(self, *args, request_obj, **kwargs):
        super().__init__(*args, **kwargs)
        self.req = request_obj
        existing = {}
        if request_obj.pk:
            for r in request_obj.requirements.select_related("characteristic"):
                existing.setdefault(r.characteristic.code, []).append(r)
        self.ambiguous_codes = {
            code
            for code, rows in existing.items()
            if len(rows) > 1
            or any(
                r.unit != r.characteristic.unit
                or r.operator not in r.characteristic.allowed_operators
                for r in rows
            )
        }
        replaced = {
            legacy: rows[0].priority
            for code, rows in existing.items()
            if code not in self.ambiguous_codes and rows[0].priority != "na"
            for legacy in LEGACY_BY_REPLACEMENT.get(code, ())
        }
        self.specs = [
            spec
            for spec in STANDARD
            if (
                spec["code"] not in LEGACY_ACCESS
                or any(
                    row.priority != "na"
                    and (
                        spec["code"] not in replaced
                        or (
                            row.priority == "required"
                            and replaced[spec["code"]] != "required"
                        )
                    )
                    for row in existing.get(spec["code"], ())
                )
            )
            if not (
                self.is_bound
                and spec["code"] + "_priority" not in self.data
                and (
                    spec["value_type"] in STRUCTURED
                    or spec["code"]
                    in {"delivery_approach", "storage_zone", "noise_limits"}
                )
            )
        ]
        for d in self.specs:
            code = d["code"]
            if code in self.ambiguous_codes:
                continue
            r = existing.get(code, [None])[0]
            self.fields[code + "_priority"] = forms.ChoiceField(
                label="Ответ" if code in GENERAL_ACCESS else "Важность",
                choices=(
                    [
                        ("na", "Нет, не требуется"),
                        ("required", "Да, обязательно"),
                        ("desired", "Да, желательно"),
                    ]
                    if code in GENERAL_ACCESS
                    else IMPORTANCE
                ),
                initial=r.priority if r else "na",
                required=d["value_type"] not in STRUCTURED
                and code not in {"delivery_approach", "storage_zone", "noise_limits"},
            )
            if d["value_type"] == "number":
                lo = r.minimum if r and r.operator in ["gte", "range", "eq"] else None
                hi = (
                    r.maximum
                    if r and r.operator == "range"
                    else (
                        r.minimum
                        if r and r.operator == "lte"
                        else lo if r and r.operator == "eq" else None
                    )
                )
                self.fields[code + "_min"] = forms.DecimalField(
                    label="От, " + d["unit"],
                    required=False,
                    min_value=0,
                    max_digits=14,
                    decimal_places=2,
                    initial=lo,
                )
                self.fields[code + "_max"] = forms.DecimalField(
                    label="До, " + d["unit"],
                    required=False,
                    min_value=0,
                    max_digits=14,
                    decimal_places=2,
                    initial=hi,
                )
            elif d["value_type"] == "boolean":
                self.fields[code + "_expected"] = forms.ChoiceField(
                    label="Что требуется?",
                    choices=[("yes", "Наличие"), ("no", "Отсутствие")],
                    initial="no" if r and not r.expected else "yes",
                    required=code not in {"delivery_approach", "storage_zone"},
                )
            elif d["value_type"] in STRUCTURED:
                self.fields[code + "_structured"] = structured_field(
                    d["value_type"],
                    code,
                    initial=r.structured_value if r else None,
                    requirement=True,
                )
                if code in GENERAL_ACCESS:
                    self.fields[code + "_structured"].label = (
                        "Особый режим входа (если нужен)"
                        if code == "visitor_entry"
                        else "Способ разгрузки (если важен)"
                    )
            else:
                self.fields[code + "_text"] = forms.CharField(
                    label="Какое значение требуется?",
                    required=False,
                    max_length=500,
                    initial=r.expected_text if r else "",
                    widget=(
                        forms.Select(choices=FLOORS)
                        if code == "floor"
                        else forms.TextInput()
                    ),
                )
        self.groups = []
        for key, title in GROUPS:
            entries = []
            for d in self.specs:
                if d["group"] != key or d["code"] in self.ambiguous_codes:
                    continue
                code = d["code"]
                entries.append(
                    {
                        "label": (
                            "Прежняя отметка о времени работы"
                            if code == "working_hours"
                            else ACCESS_QUESTIONS.get(code, d["label"])
                        ),
                        "code": code,
                        "primary": d["primary"],
                        "structured": d["value_type"] in STRUCTURED,
                        "origin": (
                            existing[code][0].origin if code in existing else "client"
                        ),
                        "fields": [
                            self[k] for k in self.fields if k.startswith(code + "_")
                        ],
                    }
                )
            self.groups.append(
                {
                    "title": title,
                    "intro": (
                        "Уточните, нужен ли вход посетителям или разгрузка, и задайте часы работы бизнеса."
                        if key == "access"
                        else ""
                    ),
                    "primary": [e for e in entries if e["primary"]],
                    "advanced": [e for e in entries if not e["primary"]],
                }
            )

    def clean(self):
        d = super().clean()
        for spec in self.specs:
            code = spec["code"]
            if code in self.ambiguous_codes or d.get(code + "_priority") in [
                None,
                "",
                "na",
            ]:
                continue
            if spec["value_type"] == "number":
                lo, hi = d.get(code + "_min"), d.get(code + "_max")
                if lo is None and hi is None:
                    self.add_error(
                        code + "_min",
                        "Укажите хотя бы одну границу или выберите «Не важно».",
                    )
                if lo is not None and hi is not None and lo > hi:
                    self.add_error(code + "_max", "Верхняя граница меньше нижней.")
            elif spec["value_type"] == "text" and not d.get(code + "_text"):
                self.add_error(
                    code + "_text", "Укажите нужное значение или выберите «Не важно»."
                )
            elif (
                spec["value_type"] in STRUCTURED
                and code not in GENERAL_ACCESS
                and not d.get(code + "_structured")
            ):
                self.add_error(
                    code + "_structured", "Выберите значение или отметьте «Не важно»."
                )
        return d

    def save(self, req):
        for spec in self.specs:
            code = spec["code"]
            if code in self.ambiguous_codes:
                continue
            priority = self.cleaned_data[code + "_priority"]
            if not priority:
                continue
            char = Characteristic.objects.get(code=code)
            old = req.requirements.filter(characteristic=char).first()
            if priority == "na" and not old:
                continue
            values = {
                "name": old.name if old else spec["label"],
                "unit": spec["unit"],
                "priority": priority,
                "operator": "eq",
                "minimum": None,
                "maximum": None,
                "expected": True,
                "expected_text": "",
            }
            if spec["value_type"] == "number":
                lo, hi = (
                    self.cleaned_data.get(code + "_min"),
                    self.cleaned_data.get(code + "_max"),
                )
                values.update(
                    operator=(
                        "range"
                        if lo is not None and hi is not None
                        else "gte" if lo is not None else "lte"
                    ),
                    minimum=lo if lo is not None else hi,
                    maximum=hi if lo is not None else None,
                )
                if priority == "na" and old:
                    values.update(
                        operator=old.operator, minimum=old.minimum, maximum=old.maximum
                    )
            elif spec["value_type"] == "boolean":
                values["expected"] = self.cleaned_data.get(code + "_expected") != "no"
            elif spec["value_type"] in STRUCTURED:
                value = self.cleaned_data.get(code + "_structured")
                if priority != "na" and not value:
                    value = GENERAL_ACCESS.get(code)
                values["structured_value"] = (
                    normalize_value(spec["value_type"], code, value, requirement=True)
                    if value
                    else old.structured_value if old else None
                )
            else:
                values["expected_text"] = self.cleaned_data.get(code + "_text", "")
            if old:
                for k, v in values.items():
                    setattr(old, k, v)
                old.save()
            else:
                Requirement.objects.create(request=req, characteristic=char, **values)


class CustomRequirementForm(forms.ModelForm):
    kind = forms.ChoiceField(
        label="Тип значения",
        choices=[
            (code, label)
            for code, label in Characteristic._meta.get_field("value_type").choices
            if code in CUSTOM_KINDS
        ],
        initial="number",
    )

    class Meta:
        model = Requirement
        fields = [
            "name",
            "kind",
            "unit",
            "operator",
            "minimum",
            "maximum",
            "expected",
            "expected_text",
            "priority",
        ]
        labels = {
            "name": "Название своего параметра",
            "operator": "Как сравнивать?",
            "minimum": "Значение / от",
            "maximum": "До",
            "expected_text": "Точное текстовое значение",
        }
        widgets = {
            "operator": forms.Select(
                choices=[
                    ("gte", "Не менее"),
                    ("lte", "Не более"),
                    ("range", "В диапазоне"),
                    ("eq", "Точно совпадает"),
                ]
            )
        }

    def __init__(self, *args, agency, **kwargs):
        super().__init__(*args, **kwargs)
        self.agency = agency
        if self.instance.pk:
            self.initial["kind"] = self.instance.characteristic.value_type

    def clean(self):
        d = super().clean()
        if not d.get("name") or d.get("DELETE"):
            return d
        if not self.instance.pk and normalized(d["name"]) in ALIASES:
            self.add_error(
                "name",
                "Этот параметр есть в стандартной карточке. Заполните его в соответствующей группе.",
            )
        if self.instance.pk and (
            d.get("kind") != self.instance.characteristic.value_type
            or (
                d.get("unit", "") != self.instance.unit
                and (
                    self.instance.characteristic.agency_id is not None
                    or d.get("unit", "") != self.instance.characteristic.unit
                )
            )
        ):
            self.add_error(
                None,
                "Тип и единица существующего параметра сохраняются. Для другой физической величины создайте новый параметр.",
            )
        if d.get("priority") != "na":
            if d.get("kind") == "number":
                if d.get("minimum") is None:
                    self.add_error("minimum", "Укажите числовой порог.")
                if d.get("operator") == "range" and (
                    d.get("maximum") is None
                    or d.get("minimum") is not None
                    and d["minimum"] > d["maximum"]
                ):
                    self.add_error("maximum", "Укажите корректную верхнюю границу.")
            elif d.get("operator") != "eq":
                self.add_error(
                    "operator", "Для наличия и текста используйте «Точно совпадает»."
                )
            elif d.get("kind") == "text" and not d.get("expected_text"):
                self.add_error("expected_text", "Укажите требуемое значение.")
        if d.get("kind") != "text":
            d["expected_text"] = ""
        if d.get("kind") != "number":
            d["minimum"] = None
            d["maximum"] = None
        return d

    def save(self, commit=True):
        obj = super().save(commit=False)
        if not obj.characteristic_id:
            obj.characteristic = resolve_characteristic(
                self.agency.pk, obj.name, obj.unit, self.cleaned_data["kind"]
            )
        if commit:
            obj.save()
        return obj


CustomRequirementsFormSet = inlineformset_factory(
    ClientRequest,
    Requirement,
    form=CustomRequirementForm,
    formset=RequirementsBase,
    extra=1,
    can_delete=False,
)


class ContextForm(forms.ModelForm):
    version = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = RequestPremises
        fields = [
            "adaptation",
            "equipment",
            "initial_stock",
            "other_once",
            "cash_reserve",
            "stress",
            "assumptions",
            "internal_notes",
        ]
        widgets = {
            "assumptions": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["version"] = self.instance.version
        if self.is_bound:
            for name in ("equipment", "initial_stock", "cash_reserve"):
                if name not in self.data:
                    self.fields[name].disabled = True


class CharacteristicForm(forms.Form):
    name = forms.CharField(label="Название характеристики", max_length=100)
    kind = forms.ChoiceField(
        label="Тип значения",
        choices=Characteristic._meta.get_field("value_type").choices,
    )
    unit = forms.CharField(
        label="Единица для числового значения", max_length=25, required=False
    )

    def clean_name(self):
        v = self.cleaned_data["name"]
        if normalized(v) in ALIASES:
            raise forms.ValidationError(
                "Используйте стандартную характеристику из списка."
            )
        return v


class CatalogFilterForm(forms.Form):
    q = forms.CharField(label="Адрес или рабочая метка", required=False, max_length=250)
    premises_type = forms.ChoiceField(
        label="Тип помещения",
        required=False,
        choices=[("", "Все типы")] + Premises._meta.get_field("premises_type").choices,
    )
    area_min = forms.DecimalField(label="Площадь от, м²", required=False, min_value=0)
    area_max = forms.DecimalField(label="Площадь до, м²", required=False, min_value=0)
    rent_max = forms.DecimalField(
        label="Базовая аренда до, ₽/мес.", required=False, min_value=0
    )
    power_min = forms.DecimalField(
        label="Подтверждённая мощность от, кВт", required=False, min_value=0
    )
    water = forms.ChoiceField(
        label="Подтверждённая вода",
        required=False,
        choices=[("", "Любые сведения"), ("yes", "Есть"), ("no", "Нет")],
    )
    ventilation = forms.ChoiceField(
        label="Подтверждённая вентиляция",
        required=False,
        choices=[("", "Любые сведения"), ("yes", "Есть"), ("no", "Нет")],
    )

    def clean(self):
        d = super().clean()
        if (
            d.get("area_min") is not None
            and d.get("area_max") is not None
            and d["area_min"] > d["area_max"]
        ):
            self.add_error("area_max", "Верхняя граница меньше нижней.")
        return d


class RecommendationFilterForm(CatalogFilterForm):
    rent_max = None
    monthly_max = forms.DecimalField(
        label="Полная стоимость до, ₽/мес.", required=False, min_value=0
    )
    status = forms.ChoiceField(
        label="Технический статус",
        required=False,
        choices=[
            ("", "Все статусы"),
            ("pass", "Соответствует"),
            ("clarify", "Требует уточнения"),
            ("blocker", "Не соответствует"),
        ],
    )
    only_confirmed = forms.BooleanField(
        label="Есть подтверждённые обязательные параметры", required=False
    )
    only_new = forms.BooleanField(label="Ещё не добавлены в сравнение", required=False)
    sort = forms.ChoiceField(
        label="Сортировка внутри группы",
        required=False,
        choices=[
            ("", "По соответствию"),
            ("score", "По рейтингу"),
            ("monthly", "Сначала ниже стоимость"),
            ("area", "Сначала больше площадь"),
        ],
    )

    def __init__(self, *args, districts=(), **kwargs):
        super().__init__(*args, **kwargs)
        if districts:
            self.fields["district"] = forms.ChoiceField(
                label="Район",
                required=False,
                choices=[("", "Все районы"), *((name, name) for name in districts)],
            )
