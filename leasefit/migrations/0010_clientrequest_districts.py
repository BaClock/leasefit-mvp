from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("leasefit", "0009_premises_geography")]

    operations = [
        migrations.AddField(
            model_name="clientrequest",
            name="districts",
            field=models.JSONField(
                "Районы подбора", blank=True, default=None, null=True
            ),
        ),
        migrations.AlterField(
            model_name="premises",
            name="location_confirmed",
            field=models.BooleanField(
                "Город и указанный район подтверждены по адресу или документам",
                default=False,
            ),
        ),
    ]
