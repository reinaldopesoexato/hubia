from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bridge", "0026_iaves"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IAvesFeedback",
            fields=[
                ("run", models.UUIDField(primary_key=True, editable=False, serialize=False)),
                ("user", models.ForeignKey(on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL)),
                ("helpful", models.BooleanField()),
                ("consulted_at", models.DateTimeField(db_index=True)),
                ("source_paths", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
