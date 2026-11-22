from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()


@register.filter
def rub(v):
    if v is None or v == "":
        return "Неизвестно"
    try:
        value = Decimal(str(v))
        places = 0 if value == value.to_integral() else 2
        return f"{value:,.{places}f}".replace(",", " ").replace(".", ",") + " ₽"
    except (InvalidOperation, ValueError):
        return str(v)


@register.filter
def positive(v):
    try:
        return Decimal(str(v)) > 0
    except (InvalidOperation, ValueError):
        return False


@register.filter
def negative(v):
    try:
        return Decimal(str(v)) < 0
    except (InvalidOperation, ValueError):
        return False


@register.filter
def ru_plural(count, forms):
    one, few, many = forms.split(",")
    if 11 <= count % 100 <= 14:
        return many
    last_digit = count % 10
    if last_digit == 1:
        return one
    return few if 2 <= last_digit <= 4 else many
