from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0002_notifications")]
    operations = [
        migrations.CreateModel(name="GitHubConnection", fields=[
            ("tenant", models.UUIDField(primary_key=True, serialize=False)),
            ("organization", models.CharField(max_length=39)),
            ("app_id", models.BigIntegerField(unique=True)),
            ("slug", models.CharField(max_length=100)),
            ("private_key", models.BinaryField()),
            ("installation_id", models.BigIntegerField(null=True, unique=True)),
            ("repositories", models.JSONField(default=list)),
            ("checked_at", models.DateTimeField(null=True)),
            ("error", models.TextField(blank=True)),
        ]),
    ]
