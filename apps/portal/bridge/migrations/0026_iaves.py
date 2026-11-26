import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bridge", "0025_link_all_tenants"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IAvesSettings",
            fields=[
                ("agent", models.UUIDField(primary_key=True, editable=False, serialize=False)),
                ("provider", models.CharField(max_length=30, blank=True, default="")),
                ("model", models.CharField(max_length=100, blank=True, default="")),
                ("encrypted_key", models.BinaryField(blank=True, default=bytes)),
            ],
        ),
        migrations.CreateModel(
            name="IAvesMemory",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("agent", models.UUIDField()),
                ("user", models.ForeignKey(on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL)),
                ("key", models.CharField(max_length=100)),
                ("value", models.TextField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=["agent", "user", "key"], name="iaves_memory_key_per_user")],
            },
        ),
    ]
