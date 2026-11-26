from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("bridge", "0016_avihub_multiple_sessions"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="PlatformMailSettings", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("address", models.EmailField(default="hubia@psxt.io", max_length=254)),
            ("sender_name", models.CharField(default="HUB IA", max_length=120)),
            ("host", models.CharField(default="smtp.hostinger.com", max_length=253)),
            ("port", models.PositiveIntegerField(default=465)),
            ("tls_mode", models.CharField(default="ssl", max_length=8)),
            ("encrypted_password", models.BinaryField(default=bytes)),
        ]),
        migrations.CreateModel(name="PasswordResetDelivery", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("encrypted_token", models.BinaryField()),
            ("due", models.DateTimeField(db_index=True)),
            ("attempts", models.PositiveSmallIntegerField(default=0)),
            ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
    ]
