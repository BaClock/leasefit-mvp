from django.db import migrations


CHARACTERISTICS = (
    ("visitor_entry", "Как посетители должны попадать в помещение?", "access", "enum"),
    (
        "operating_schedule",
        "В какое время бизнес должен иметь возможность работать?",
        "access",
        "time_schedule",
    ),
    (
        "unloading_methods",
        "Какой способ разгрузки необходим?",
        "access",
        "multi_select",
    ),
    ("delivery_vehicles", "Тип транспорта для поставок", "access", "multi_select"),
    ("delivery_schedule", "Разрешённые часы поставок", "access", "time_schedule"),
    ("delivery_approach", "Подъезд непосредственно к входу", "access", "boolean"),
    ("noise_limits", "Ограничения по шуму", "access", "text"),
    ("storage_zone", "Зона хранения", "layout", "boolean"),
)


def add_characteristics(apps, schema_editor):
    characteristic = apps.get_model("leasefit", "Characteristic")
    for code, label, group, value_type in CHARACTERISTICS:
        characteristic.objects.using(schema_editor.connection.alias).get_or_create(
            code=code,
            defaults={
                "label": label,
                "group": group,
                "value_type": value_type,
                "unit": "",
                "allowed_operators": ["eq"],
            },
        )


def remove_unused_characteristics(apps, schema_editor):
    alias = schema_editor.connection.alias
    fields = (
        apps.get_model("leasefit", "Characteristic")
        .objects.using(alias)
        .filter(
            code__in=[code for code, _, _, _ in CHARACTERISTICS], agency__isnull=True
        )
    )
    for model_name in ("Requirement", "Observation"):
        if (
            apps.get_model("leasefit", model_name)
            .objects.using(alias)
            .filter(characteristic__in=fields)
            .exists()
        ):
            raise RuntimeError(
                "Нельзя откатить справочник с сохранёнными структурированными данными."
            )
    fields.delete()


class Migration(migrations.Migration):
    dependencies = [("leasefit", "0007_expanded_mvp")]
    operations = [
        migrations.RunPython(add_characteristics, remove_unused_characteristics)
    ]
