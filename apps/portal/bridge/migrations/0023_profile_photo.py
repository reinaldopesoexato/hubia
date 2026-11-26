from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("bridge", "0022_avihub_setup_deferred"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="ProfilePhoto",
            fields=[
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True,
                                              serialize=False, to=settings.AUTH_USER_MODEL)),
                ("image", models.BinaryField(null=True)),
                ("google_url", models.URLField(max_length=2048, blank=True, default="")),
            ],
        ),
    ]
