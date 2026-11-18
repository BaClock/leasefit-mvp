from decimal import Decimal

from django import forms


def money(label):
    return forms.DecimalField(
        label=label, required=False, min_value=0, max_digits=14, decimal_places=2
    )


class FinancialPlanForm(forms.Form):
    version = forms.IntegerField(widget=forms.HiddenInput)
    revenue = money("Сценарная выручка бизнеса, ₽/мес.")
    average = money("Средний чек, ₽")
    variable = money("Переменные затраты на одну операцию, ₽")
    fixed = money("Постоянные расходы бизнеса без расходов помещения, ₽/мес.")
    working_days = forms.IntegerField(
        label="Рабочие дни в месяц", required=False, min_value=1, max_value=31
    )
    target_profit = money("Целевой операционный остаток, ₽/мес.")
    custom_drop = forms.DecimalField(
        label="Пользовательское снижение продаж, %",
        required=False,
        min_value=0,
        max_value=100,
        max_digits=5,
        decimal_places=2,
    )
    custom_fixed = money(
        "Дополнительные постоянные затраты в пользовательском сценарии, ₽/мес."
    )

    def __init__(self, *args, request_obj, **kwargs):
        super().__init__(
            *args,
            initial={**request_obj.financial_plan, "version": request_obj.version},
            **kwargs
        )
        if request_obj.business_format.startswith("pickup."):
            self.fields["average"].label = "Вознаграждение за операцию ПВЗ, ₽"

    def plan(self):
        return {
            name: str(value) if isinstance(value, Decimal) else value
            for name, value in self.cleaned_data.items()
            if name != "version" and value is not None
        }
