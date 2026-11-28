import uuid
from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from .listing_filters import CATEGORIES


class Agency(models.Model):
    name = models.CharField("Агентство", max_length=160)
    parser_paused = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Membership(models.Model):
    ROLES = [("realtor", "Риелтор"), ("manager", "Руководитель")]
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="membership"
    )
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    role = models.CharField(max_length=12, choices=ROLES, default="realtor")
    active = models.BooleanField(default=True)


class Invitation(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE)
    email = models.EmailField()
    role = models.CharField(max_length=12, choices=Membership.ROLES, default="realtor")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)

    @property
    def valid(self):
        return not self.accepted_at and self.expires_at > timezone.now()


money = dict(
    max_digits=14,
    decimal_places=2,
    null=True,
    blank=True,
    validators=[MinValueValidator(0)],
)


class ClientRequest(models.Model):
    STAGES = [("draft", "Черновик"), ("active", "В работе"), ("done", "Завершён")]
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField("Название запроса", max_length=180)
    city = models.CharField("Город", max_length=120, default="Новосибирск")
    business = models.CharField("Формат бизнеса", max_length=160, default="Кофейня")
    business_format = models.CharField(max_length=40, blank=True)
    business_answers = models.JSONField(default=dict, blank=True)
    financial_plan = models.JSONField(default=dict, blank=True)
    geography = models.CharField("Районы / ориентиры", max_length=250, blank=True)
    districts = models.JSONField("Районы подбора", null=True, blank=True, default=None)
    budget = models.DecimalField("Лимит в месяц, ₽", **money)
    start_budget = models.DecimalField("Бюджет на старт, ₽", **money)
    stage = models.CharField(max_length=10, choices=STAGES, default="draft")
    archived = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def ready(self):
        return self.requirements.filter(priority="required").exists()

    premises = models.ManyToManyField(
        "Premises", through="RequestPremises", related_name="requests"
    )
    report_parameters = models.JSONField(default=list, blank=True)
    selection_version = models.PositiveIntegerField(default=1)

    def __str__(self):
        return self.title

    @property
    def business_format_label(self):
        from .business import FORMATS

        spec = FORMATS.get(self.business_format)
        return spec.label if spec else self.business


class Characteristic(models.Model):
    code = models.SlugField(max_length=90, unique=True)
    label = models.CharField(max_length=100)
    group = models.CharField(
        max_length=20,
        choices=[
            ("layout", "Площадь и планировка"),
            ("engineering", "Инженерные коммуникации"),
            ("access", "Условия доступа и работы помещения"),
            ("specialized", "Складские и производственные характеристики"),
            ("custom", "Дополнительные требования"),
        ],
    )
    value_type = models.CharField(
        max_length=16,
        choices=[
            ("number", "Число"),
            ("boolean", "Да / нет"),
            ("text", "Текст"),
            ("enum", "Вариант"),
            ("multi_select", "Несколько вариантов"),
            ("time_schedule", "График"),
        ],
    )
    unit = models.CharField(max_length=25, blank=True)
    allowed_operators = models.JSONField(default=list)
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, null=True, blank=True)

    def __str__(self):
        return self.label


class Requirement(models.Model):
    PRIORITIES = [
        ("required", "Обязательное"),
        ("desired", "Желательное"),
        ("na", "Не применимо"),
    ]
    OPERATORS = [("gte", "≥"), ("lte", "≤"), ("range", "Диапазон"), ("eq", "Точно")]
    request = models.ForeignKey(
        ClientRequest, on_delete=models.CASCADE, related_name="requirements"
    )
    characteristic = models.ForeignKey(Characteristic, on_delete=models.PROTECT)
    name = models.CharField("Параметр", max_length=100)
    expected_text = models.CharField("Требуемое значение", max_length=500, blank=True)
    structured_value = models.JSONField(null=True, blank=True)
    origin = models.CharField(max_length=80, default="client")
    unit = models.CharField("Единица", max_length=25, blank=True)
    operator = models.CharField(
        "Оператор", max_length=8, choices=OPERATORS, default="gte"
    )
    minimum = models.DecimalField("Порог / от", **money)
    maximum = models.DecimalField("До", **money)
    expected = models.BooleanField("Требуется «да»", default=True)
    priority = models.CharField(
        "Важность", max_length=10, choices=PRIORITIES, default="required"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "name"], name="one_requirement_per_field"
            )
        ]

    def __str__(self):
        if self.operator == "eq":
            from .operations import STRUCTURED, value_label

            if self.characteristic.value_type in STRUCTURED:
                value = value_label(
                    self.characteristic.value_type,
                    self.characteristic.code,
                    self.structured_value,
                    requirement=True,
                )
                return f"{self.name}: {value or 'не задано'}"
            if self.characteristic.value_type == "boolean":
                return f"{self.name}: {'да' if self.expected else 'нет'}"
            if self.characteristic.value_type == "text":
                return f"{self.name}: {self.expected_text}"
        val = (
            f"{self.minimum}–{self.maximum}"
            if self.operator == "range"
            else str(self.minimum)
        )
        return f"{self.name}: {self.get_operator_display()} {val} {self.unit}"

    def save(self, *args, **kwargs):
        if not self.characteristic_id:
            from .characteristics import resolve_characteristic

            self.characteristic = resolve_characteristic(
                self.request.agency_id,
                self.name,
                self.unit,
                (
                    "boolean"
                    if self.operator == "eq" and not self.expected_text
                    else "text" if self.expected_text else "number"
                ),
            )
        return super().save(*args, **kwargs)


class Premises(models.Model):
    agency = models.ForeignKey(
        Agency, on_delete=models.PROTECT, related_name="premises"
    )
    premises_type = models.CharField(
        "Тип помещения",
        max_length=30,
        choices=[
            ("unknown", "Не указан"),
            ("retail", "Торговое"),
            ("office", "Офис"),
            ("food", "Общепит"),
            ("warehouse", "Склад"),
            ("service", "Услуги"),
            ("other", "Другое"),
        ],
        default="unknown",
    )
    lease_terms = models.TextField("Подтверждённые общие условия договора", blank=True)
    label = models.CharField("Адрес или рабочая метка", max_length=250)
    city = models.CharField("Город", max_length=120, blank=True)
    district = models.CharField("Район", max_length=120, blank=True)
    location_confirmed = models.BooleanField(
        "Город и указанный район подтверждены по адресу или документам", default=False
    )
    floor = models.CharField("Этаж", max_length=40, blank=True)
    number = models.CharField("Номер помещения", max_length=60, blank=True)
    listing_url = models.URLField(
        "Ссылка на объявление (источник)", blank=True, max_length=1500
    )
    commercial_source_type = models.CharField(
        "Источник коммерческих условий",
        max_length=12,
        choices=[
            ("listing", "Объявление"),
            ("owner", "Ответ собственника"),
            ("document", "Документ"),
            ("manual", "Ручной ввод"),
        ],
        default="manual",
    )
    commercial_source = models.CharField(
        "Источник всех введённых сумм и расчётной площади, страница / раздел",
        max_length=500,
        blank=True,
    )
    commercial_date = models.DateField(
        "Дата коммерческих условий", null=True, blank=True
    )
    commercial_status = models.CharField(
        "Подтверждение коммерческих условий",
        max_length=12,
        choices=[
            ("unverified", "Не подтверждено"),
            ("confirmed", "Подтверждено"),
            ("conflict", "Конфликт"),
        ],
        default="unverified",
    )
    commercial_confirmation = models.CharField(
        "Основание подтверждения коммерческих условий", max_length=700, blank=True
    )
    area = models.DecimalField("Площадь, м² (для расчёта аренды)", **money)
    rent = models.DecimalField("Базовая аренда, ₽/мес.", **money)
    rate = models.DecimalField("Ставка, ₽/м² в месяц", **money)
    operating = models.DecimalField("Эксплуатация, ₽/мес.", **money)
    utilities = models.DecimalField("Коммунальные, ₽/мес.", **money)
    other_monthly = models.DecimalField("Прочие регулярные, ₽/мес.", **money)
    vat_mode = models.CharField(
        "НДС",
        max_length=12,
        choices=[
            ("unknown", "Условия неизвестны"),
            ("included", "Включён / не начисляется"),
            ("excluded", "Сверх базовой аренды"),
        ],
        default="unknown",
    )
    vat_rate = models.DecimalField(
        "НДС сверх базы, %",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    deposit = models.DecimalField("Депозит, ₽ (отдельно от месяца)", **money)
    deposit_refundable = models.BooleanField(
        "Возвратность депозита подтверждена", default=False
    )
    commission = models.DecimalField("Комиссия, ₽", **money)
    listing_conflicts = models.JSONField(default=list, blank=True)
    availability_confirmed_on = models.DateField("Помещение доступно на дату", null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.label


class PremisesListing(models.Model):
    premises = models.OneToOneField(
        Premises, on_delete=models.CASCADE, primary_key=True, related_name="listing"
    )
    source = models.CharField(max_length=40)
    source_id = models.CharField(max_length=80)
    url = models.URLField(max_length=1500, blank=True)
    description = models.TextField(blank=True)
    photos = models.JSONField(default=list, blank=True)
    projected = models.JSONField(default=dict, blank=True)
    title = models.CharField(max_length=250, blank=True)
    address = models.CharField(max_length=250, blank=True)
    categories = models.JSONField(default=list, blank=True)
    details = models.JSONField(default=dict, blank=True)


class ListingPublication(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    premises = models.ForeignKey(
        Premises, on_delete=models.CASCADE, related_name="publications"
    )
    source = models.CharField(max_length=40)
    source_id = models.CharField(max_length=80)
    group_id = models.CharField(max_length=120, blank=True)
    version = models.PositiveIntegerField(default=0)
    snapshot = models.JSONField(default=dict)
    checked_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    next_check_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "source", "source_id"],
                name="unique_agency_listing_publication",
            )
        ]
        indexes = [models.Index(fields=["agency", "group_id"], name="listing_agency_group_idx")]


class ListingPublicationVersion(models.Model):
    publication = models.ForeignKey(
        ListingPublication, on_delete=models.CASCADE, related_name="versions"
    )
    number = models.PositiveIntegerField()
    captured_at = models.DateTimeField(default=timezone.now)
    snapshot = models.JSONField()
    changes = models.JSONField(default=list)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["publication", "number"], name="unique_listing_publication_version"
            )
        ]


class ParserConfiguration(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="parser_configurations")
    category = models.CharField(max_length=20, choices=CATEGORIES)
    enabled = models.BooleanField(default=True)
    interval_minutes = models.PositiveIntegerField(default=60)
    offset_minutes = models.PositiveIntegerField(default=0)
    filters = models.JSONField(default=dict, blank=True)
    priority = models.PositiveIntegerField(default=10)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(db_index=True)
    cursor = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agency", "category"], name="unique_agency_parser_category")]


class ParserRun(models.Model):
    STATUSES = [
        ("running", "Выполняется"), ("complete", "Завершено"), ("partial", "Частичная загрузка"),
        ("error", "Ошибка API"), ("quota", "Достигнут лимит запросов"),
    ]
    configuration = models.ForeignKey(ParserConfiguration, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=12, choices=STATUSES, default="running")
    scheduled_at = models.DateTimeField()
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=350, blank=True)


class RequestPremises(models.Model):
    request = models.ForeignKey(
        ClientRequest, on_delete=models.CASCADE, related_name="premises_links"
    )
    premises = models.ForeignKey(
        Premises, on_delete=models.PROTECT, related_name="request_links"
    )
    adaptation = models.DecimalField("Ремонт / адаптация для клиента, ₽", **money)
    other_once = models.DecimalField("Прочие стартовые затраты клиента, ₽", **money)
    equipment = models.DecimalField("Оборудование, ₽", **money)
    initial_stock = models.DecimalField("Первоначальные закупки, ₽", **money)
    cash_reserve = models.DecimalField("Кассовый резерв, ₽", **money)
    regulatory_reviews = models.JSONField(default=dict, blank=True)
    stress = models.DecimalField(
        "Допущение клиента: увеличение расходов, ₽/мес.",
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    assumptions = models.TextField("Допущения для этого клиента", blank=True)
    internal_notes = models.TextField(
        "Внутренние заметки запроса (не попадут в PDF)", blank=True
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "premises"], name="unique_request_premises"
            )
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        if (
            self.request_id
            and self.premises_id
            and self.request.agency_id != self.premises.agency_id
        ):
            raise ValidationError(
                "Помещение и запрос должны принадлежать одному агентству."
            )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class Observation(models.Model):
    SCOPES = [
        ("premises", "Помещение / блок"),
        ("room", "Отдельная комната"),
        ("building", "Здание"),
        ("reference", "Справочный лот"),
    ]
    SOURCES = [
        ("listing", "Объявление"),
        ("manual", "Ручной ввод"),
        ("owner", "Ответ собственника"),
        ("document", "Документ"),
        ("archive", "Архивный документ"),
    ]
    STATUSES = [
        ("unknown", "Неизвестно"),
        ("unverified", "Не подтверждено"),
        ("confirmed", "Подтверждено"),
        ("conflict", "Конфликт"),
    ]
    premises = models.ForeignKey(
        Premises, on_delete=models.CASCADE, related_name="observations"
    )
    characteristic = models.ForeignKey(Characteristic, on_delete=models.PROTECT)
    document = models.ForeignKey(
        "Attachment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="observations",
    )
    name = models.CharField("Параметр", max_length=100)
    value = models.DecimalField("Числовое значение", **money)
    boolean = models.BooleanField(
        "Да / нет (пусто = неизвестно)", null=True, blank=True
    )
    text = models.CharField(
        "Текст / дополнительное значение", max_length=500, blank=True
    )
    structured_value = models.JSONField(null=True, blank=True)
    unit = models.CharField("Единица", max_length=25, blank=True)
    scope = models.CharField(
        "Уровень сведений", max_length=12, choices=SCOPES, default="premises"
    )
    locator = models.CharField("Комната / часть объекта", max_length=200, blank=True)
    source_type = models.CharField(
        "Тип источника", max_length=12, choices=SOURCES, default="manual"
    )
    source = models.CharField("Источник, страница / раздел", max_length=500)
    source_url = models.URLField("Ссылка на источник", max_length=1500, blank=True)
    observed_on = models.DateField("Дата сведений")
    status = models.CharField(
        "Подтверждение", max_length=12, choices=STATUSES, default="unverified"
    )
    confirmation = models.CharField(
        "Основание подтверждения / разрешения конфликта", max_length=700, blank=True
    )
    superseded = models.BooleanField(default=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)

    @property
    def structure_display(self):
        from .operations import value_label

        if self.structured_value is not None:
            return value_label(
                self.characteristic.value_type,
                self.characteristic.code,
                self.structured_value,
            )
        return ""

    def save(self, *args, **kwargs):
        if not self.characteristic_id:
            from .characteristics import resolve_characteristic

            self.characteristic = resolve_characteristic(
                self.premises.agency_id,
                self.name,
                self.unit,
                (
                    "number"
                    if self.value is not None
                    else "boolean" if self.boolean is not None else "text"
                ),
            )
        return super().save(*args, **kwargs)


class Evaluation(models.Model):
    request = models.ForeignKey(
        ClientRequest, on_delete=models.PROTECT, related_name="evaluations"
    )
    context_version = models.PositiveIntegerField(default=1)
    link_identity = models.PositiveBigIntegerField(default=0)
    premises = models.ForeignKey(
        Premises, on_delete=models.CASCADE, related_name="evaluations"
    )
    profile_version = models.PositiveIntegerField()
    premises_version = models.PositiveIntegerField()
    rules_version = models.CharField(max_length=20, default="p0-1")
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    @property
    def current(self):
        return (
            self.profile_version == self.request.version
            and self.premises_version == self.premises.version
            and RequestPremises.objects.filter(
                pk=self.link_identity,
                request_id=self.request_id,
                premises_id=self.premises_id,
                version=self.context_version,
            ).exists()
        )


class Attachment(models.Model):
    origin_request = models.ForeignKey(
        ClientRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legacy_attachments",
    )
    premises = models.ForeignKey(
        Premises, on_delete=models.CASCADE, related_name="attachments"
    )
    filename = models.CharField(max_length=250)
    content_type = models.CharField(max_length=60)
    data = models.BinaryField()
    sha256 = models.CharField(max_length=64)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["premises", "sha256"], name="unique_attachment"
            )
        ]


class Report(models.Model):
    request = models.ForeignKey(
        ClientRequest, on_delete=models.PROTECT, related_name="reports"
    )
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    pdf = models.BinaryField()
    fingerprint = models.CharField(max_length=64)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "version"], name="unique_report_version"
            ),
            models.UniqueConstraint(
                fields=["request", "fingerprint"], name="unique_report_snapshot"
            ),
        ]


class PilotApplication(models.Model):
    agency = models.CharField("Название агентства", max_length=160)
    city = models.CharField("Город", max_length=120)
    person = models.CharField("Контактное лицо", max_length=160)
    contact = models.CharField("Рабочий email или телефон", max_length=160)
    types = models.CharField(
        "Типы помещений (необязательно)", max_length=250, blank=True
    )
    comment = models.TextField("Комментарий (необязательно)", blank=True)
    consent = models.BooleanField("Согласен на обработку заявки", default=False)
    creation_key = models.UUIDField(default=uuid.uuid4, unique=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    status = models.CharField("Состояние", max_length=30, default="Новая")
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    resource = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
