from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("leasefit", "0010_clientrequest_districts")]

    operations = [
        migrations.CreateModel(
            name="PremisesListing",
            fields=[
                (
                    "premises",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="listing",
                        serialize=False,
                        to="leasefit.premises",
                    ),
                ),
                ("source", models.CharField(max_length=40)),
                ("source_id", models.CharField(max_length=80)),
                ("url", models.URLField(blank=True, max_length=1500)),
                ("description", models.TextField(blank=True)),
                ("photos", models.JSONField(blank=True, default=list)),
            ],
        ),
    ]
