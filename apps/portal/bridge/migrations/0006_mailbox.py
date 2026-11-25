import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0005_catalog_alerts"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="MailProvider", fields=[
            ("tenant", models.UUIDField(primary_key=True, serialize=False)),
            ("domain", models.CharField(max_length=253)),
            ("enabled", models.BooleanField(default=True)),
        ]),
        migrations.CreateModel(name="Mailbox", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("provider", models.ForeignKey(to="bridge.mailprovider", on_delete=models.CASCADE)),
            ("user", models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ("address", models.EmailField(max_length=254)),
            ("password_cipher", models.BinaryField()),
            ("links", models.ManyToManyField(to="bridge.link", blank=True)),
        ], options={"constraints":[models.UniqueConstraint(fields=["provider","user"], name="mailbox_per_member")]}),
    ]
