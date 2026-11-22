"""Metric methods and calculations using only the request and premises inputs."""

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import NamedTuple


class Method(NamedTuple):
    name: str
    stage: str
    value: str
    inputs: str
    formula: str
    unit: str
    limits: str
    version: str = "1"


METHODS = {
    "M01": Method(
        "Критические ограничения",
        "P0",
        "Определить, что выяснить до подписания договора.",
        "Требования клиента, наблюдения, предварительный нормативный чек-лист.",
        "Подтверждённое расхождение блокирует обязательное условие. Пробел или конфликт требуют проверки.",
        "Статус",
        "Нормативные пункты проверяются отдельно по документам.",
    ),
    "M02": Method(
        "Предельная допустимая аренда",
        "P0",
        "Определить верхнюю ставку для переговоров.",
        "Бюджет клиента, расходы помещения, сценарная выручка и затраты.",
        "Предел включает вычет прочих ежемесячных расходов и пересчёт НДС. Экономический предел дополнительно вычитает переменные и фиксированные затраты бизнеса и целевой остаток.",
        "₽/мес.",
        "Бюджетный и сценарный пределы рассчитываются независимо. При пропуске необходимой статьи результат отсутствует.",
    ),
    "M03": Method(
        "Нагрузка помещения",
        "P0",
        "Сравнить предложения по регулярным расходам.",
        "Аренда, эксплуатация, коммунальные, прочие платежи, НДС, сценарная выручка.",
        "Сумма известных ежемесячных начислений. Доля = полная нагрузка / выручка × 100.",
        "₽/мес. и %",
        "При пропусках выводится только известный подытог. Доля требует полной нагрузки и положительной выручки.",
    ),
    "M04": Method(
        "Точка безубыточности",
        "P0",
        "Оценить минимальное число операций для покрытия расходов.",
        "Средняя выручка и переменный расход на операцию, фиксированные расходы, полная нагрузка помещения, рабочие дни.",
        "Операции = округление вверх (фиксированные расходы с помещением / маржа на операцию). Дневной порог округляется вверх.",
        "Операции/мес. и операции/день",
        "Нужна положительная маржа. Для ПВЗ выручка на операцию равна вознаграждению.",
    ),
    "M05": Method(
        "Деньги на открытие",
        "P0",
        "Проверить достаточность стартового бюджета.",
        "Депозит, комиссия, адаптация, оборудование, закупки, прочие затраты, заданный кассовый резерв.",
        "Стартовая потребность — сумма разовых затрат и резерва.",
        "₽",
        "При пропусках известен подытог. Без календаря платежей кассовый дефицит не рассчитывается.",
    ),
    "M06": Method(
        "Устойчивость к падению продаж",
        "P0",
        "Понять, при каком снижении операций результат станет отрицательным.",
        "Сценарная выручка, выручка и переменные затраты на операцию, фиксированные расходы, нагрузка помещения, параметры стресса.",
        "Объём = выручка / цена операции. Результат = объём × маржа − фиксированные расходы. Допустимое падение = запас до безубыточности / базовый объём × 100.",
        "₽/мес., операции и %",
        "Сценарий меняет объём, сохраняя переменную сумму на операцию. Рост фиксированных затрат применяется только в пользовательском сценарии.",
    ),
    "M07": Method(
        "Локальный спрос",
        "P1",
        "Оценить доступный спрос вокруг адреса.",
        "Зона охвата и демографические/поисковые данные.",
        "Агрегировать аудиторию внутри достижимой зоны.",
        "Человек/запросы",
        "Нужны границы охвата и сопоставимые данные.",
    ),
    "M08": Method(
        "Проходимость",
        "P1",
        "Сравнить фактический поток около входов.",
        "Замеры по часам и дням.",
        "Сопоставить медианы потока за одинаковые периоды.",
        "Чел./час",
        "Без замеров трафик неизвестен.",
    ),
    "M09": Method(
        "Конкурентная среда",
        "P1",
        "Оценить насыщенность выбранной категорией.",
        "Точки конкурентов, время пути, границы охвата.",
        "Подсчитать действующих конкурентов в зоне охвата.",
        "Точки",
        "Нужен актуальный геокаталог.",
    ),
    "M10": Method(
        "Видимость и доступность входа",
        "P1",
        "Проверить доступность входа для нужной аудитории.",
        "Полевой аудит входа, подходов и видимости вывески.",
        "Сопоставить признаки маршрутов с требованиями пользователя.",
        "Чек-лист",
        "Полевой аудит отсутствует. Отдельные факты доступны в карточке.",
    ),
    "M11": Method(
        "Ставка относительно рынка",
        "P1",
        "Подготовить обоснование для переговоров об аренде.",
        "Проверенные аналоги по району, площади, условиям сделки и периоду.",
        "Сравнить нормализованную ставку с медианой сопоставимых объявлений или сделок.",
        "₽/м²/мес. и %",
        "Без аналогов сравнение не выполняется.",
    ),
    "M12": Method(
        "Объяснимое соответствие бизнесу",
        "P0",
        "Расставить приоритет в рекомендациях и при сравнении вариантов.",
        "Подтверждённые требования и месячный бюджет.",
        "Обязательный параметр даёт 3, желательный — 1, бюджет — 2. Доля от максимума переводится на шкалу 0–100. При неполных расходах бюджет учитывается, только если подтверждённый подытог уже превышает лимит.",
        "Баллы из 100",
        "Один вес на характеристику. Статус обязательных условий определяет порядок перед баллом. Без технических подтверждений балл отсутствует. Нормативные отметки учитываются отдельно.",
        "2",
    ),
    "M13": Method(
        "Сезонный профиль и динамика спроса",
        "P1",
        "Подготовить сезонный резерв после накопления наблюдений.",
        "Помесячная выручка и трафик за сопоставимые периоды.",
        "Выделить сезонные индексы после проверки полноты рядов.",
        "Индекс",
        "Многолетних рядов пока нет.",
    ),
    "M14": Method(
        "Достоверность данных",
        "P0",
        "Выбрать, какие подтверждения запросить у собственника.",
        "Активные обязательные проверки, документы, даты и область наблюдений.",
        "Подсчитать подтверждённые, неподтверждённые и неизвестные критические характеристики.",
        "Характеристики",
        "Подтверждение требует актуального источника по помещению. Конфликты относятся к неподтверждённым.",
    ),
    "M15": Method(
        "Квалифицированный спрос на объект",
        "P1",
        "Отличить интерес целевых арендаторов от случайных просмотров.",
        "Заявки CRM, критерии арендаторов, дедуплицированные объекты.",
        "Подсчитать уникальные подходящие запросы за период.",
        "Запросы/период",
        "Нужны квалифицированные заявки и история.",
    ),
    "M16": Method(
        "Время до подтверждённой аренды",
        "P2",
        "Планировать срок экспозиции и простой.",
        "Даты публикаций, прекращения экспозиции и подтверждённых договоров.",
        "Оценить время до договора с учётом ещё открытых объектов.",
        "Дни",
        "Нужны сопоставимая история и сведения о договорах.",
    ),
    "M17": Method(
        "Доход собственника с учётом уступок",
        "P1",
        "Сравнить условия аренды после каникул и скидок.",
        "График аренды, каникулы, скидки и платёжный календарь.",
        "Суммировать поступления и разделить на весь срок.",
        "₽ и ₽/мес.",
        "Нужен график платежей по договору.",
    ),
    "M18": Method(
        "Каннибализация собственной сети",
        "P2",
        "Оценить перераспределение спроса между точками сети.",
        "Зоны охвата действующих точек, данные о клиентах и продажах.",
        "Сопоставить перекрытие зон и изменение продаж относительно базового периода.",
        "%",
        "Перекрытие зон само по себе не доказывает потерю продаж.",
    ),
}
INSUFFICIENT = "Недостаточно данных для расчёта"


def amount(value):
    return Decimal(value) if value is not None and value != "" else None


def rounded(value):
    return value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def opening_costs(p, context):
    if context is None:
        return []
    return [
        ("Депозит", p.deposit),
        ("Комиссия", p.commission),
        ("Адаптация", context.adaptation),
        ("Оборудование", context.equipment),
        ("Первоначальные закупки", context.initial_stock),
        ("Прочие затраты", context.other_once),
        ("Кассовый резерв", context.cash_reserve),
    ]


def financial_metrics(req, p, context, costs):
    plan = req.financial_plan
    revenue, average, variable, fixed = (
        amount(plan.get(key)) for key in ("revenue", "average", "variable", "fixed")
    )
    fixed_total = None
    if fixed is not None and costs["complete"]:
        fixed_total = fixed + amount(costs["monthly_total"])
    margin = (
        average - variable if average is not None and variable is not None else None
    )
    volume = (
        revenue / average
        if revenue is not None and average is not None and average > 0
        else None
    )
    days = plan.get("working_days")
    result = {
        "profit_rent": None,
        "occupancy_share": (
            rounded(100 * amount(costs["monthly_total"]) / revenue)
            if costs["complete"] and revenue and revenue > 0
            else None
        ),
        "break_even": None,
        "daily_break_even": None,
        "margin": margin,
        "scenarios": [],
        "max_drop": None,
    }
    fit_costs = opening_costs(p, context)
    result["opening"] = sum((v for _, v in fit_costs if v is not None), Decimal(0))
    result["opening_missing"] = [label for label, value in fit_costs if value is None]
    result["opening_gap"] = (
        result["opening"] - req.start_budget
        if req.start_budget is not None and fit_costs
        else None
    )
    if volume is not None and fixed is not None and margin is not None:
        result["profit_rent"] = economic_rent(
            p, volume * margin - fixed - amount(plan.get("target_profit", 0))
        )
    base = amount(costs["base"])
    for code, limit in (
        ("budget_rent_gap", amount(costs["allowed_rent"])),
        ("profit_rent_gap", result["profit_rent"]),
    ):
        result[code] = (
            rounded(base - limit) if base is not None and limit is not None else None
        )
    if margin is not None and margin > 0 and fixed_total is not None:
        threshold = fixed_total / margin
        result["break_even"] = int(threshold.to_integral_value(rounding=ROUND_CEILING))
        if days:
            result["daily_break_even"] = int(
                (threshold / days).to_integral_value(rounding=ROUND_CEILING)
            )
    if volume is not None and margin is not None and fixed_total is not None:
        scenarios = [
            ("Базовый", Decimal(0), Decimal(0)),
            ("Снижение на 10%", Decimal(10), Decimal(0)),
            ("Снижение на 20%", Decimal(20), Decimal(0)),
        ]
        if plan.get("custom_drop") is not None or plan.get("custom_fixed") is not None:
            scenarios.append(
                (
                    "Пользовательский",
                    amount(plan.get("custom_drop", 0)),
                    amount(plan.get("custom_fixed", 0)),
                )
            )
        for label, percent, extra_fixed in scenarios:
            units = volume * (1 - percent / 100)
            result["scenarios"].append(
                {
                    "label": label,
                    "percent": percent,
                    "profit": rounded(units * margin - fixed_total - extra_fixed),
                    "buffer": (
                        rounded(units - (fixed_total + extra_fixed) / margin)
                        if margin > 0
                        else None
                    ),
                }
            )
        if volume > 0 and margin > 0:
            result["max_drop"] = rounded(
                max(Decimal(0), 100 * (volume - fixed_total / margin) / volume)
            )
    return result


def economic_rent(p, available):
    fixed = [p.operating, p.utilities, p.other_monthly]
    if (
        any(number is None for number in fixed)
        or p.vat_mode == "unknown"
        or (p.vat_mode == "excluded" and p.vat_rate is None)
    ):
        return None
    remainder = available - sum(fixed, Decimal(0))
    if remainder > 0 and p.vat_mode == "excluded":
        remainder /= 1 + p.vat_rate / 100
    return rounded(remainder)


def evidence_summary(rows):
    groups = {}
    for row in rows:
        if row["priority"] != "required":
            continue
        state = (
            "confirmed"
            if row["status"] != "clarify"
            else (
                "unconfirmed"
                if any(
                    source.get("has_value") and not source["superseded"]
                    for source in row["sources"]
                )
                else "unknown"
            )
        )
        states = groups.setdefault(row["characteristic_id"], set())
        states.add(state)
    result = {"confirmed": 0, "unconfirmed": 0, "unknown": 0}
    for states in groups.values():
        result[
            next(
                state
                for state in ("unknown", "unconfirmed", "confirmed")
                if state in states
            )
        ] += 1
    return result
