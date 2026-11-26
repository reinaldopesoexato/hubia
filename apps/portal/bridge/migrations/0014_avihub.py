from uuid import uuid4
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0013_outbound_email_activity")]
    operations = [
        migrations.CreateModel(name="AviHubAgent", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid4, editable=False, serialize=False)),
            ("tenant", models.UUIDField(unique=True)),
            ("origin", models.URLField()),
        ]),
        migrations.CreateModel(name="AviHubSession", fields=[
            ("id", models.BigAutoField(primary_key=True, auto_created=True, serialize=False, verbose_name="ID")),
            ("agent", models.ForeignKey(to="bridge.avihubagent", on_delete=models.CASCADE)),
            ("user", models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ("encrypted", models.BinaryField()),
            ("ready", models.BooleanField(default=False)),
            ("expires", models.DateTimeField()),
        ], options={"constraints":[models.UniqueConstraint(fields=["agent","user"], name="avihub_personal_session")]}),
    ]
