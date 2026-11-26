from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def keep_active_sessions(apps, schema_editor):
    sessions = apps.get_model("bridge", "AviHubSession").objects.using(schema_editor.connection.alias)
    sessions.filter(ready=True, expires__gt=timezone.now()).update(expires=None)


def restore_time_limit(apps, schema_editor):
    sessions = apps.get_model("bridge", "AviHubSession").objects.using(schema_editor.connection.alias)
    sessions.filter(expires__isnull=True).update(expires=timezone.now() + timedelta(hours=8))


class Migration(migrations.Migration):
    dependencies = [("bridge", "0014_avihub")]
    operations = [
        migrations.AlterField(model_name="avihubsession", name="expires", field=models.DateTimeField(null=True)),
        migrations.RunPython(keep_active_sessions, restore_time_limit),
    ]
