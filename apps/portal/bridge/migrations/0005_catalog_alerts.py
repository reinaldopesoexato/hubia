from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0004_note_library")]
    operations = [
        migrations.CreateModel(name="CatalogAlerts", fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
            ("tenant", models.UUIDField()),
            ("space", models.UUIDField()),
        ], options={"constraints":[models.UniqueConstraint(fields=["tenant","space"],name="catalog_alerts_space")]}),
        migrations.AddField(model_name="ticket", name="notice_agent", field=models.UUIDField(null=True)),
        migrations.AddField(model_name="ticket", name="notice_due", field=models.DateTimeField(null=True)),
        migrations.AddField(model_name="ticket", name="notice_attempts", field=models.PositiveSmallIntegerField(default=0)),
    ]
