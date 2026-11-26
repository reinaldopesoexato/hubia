from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0021_connection_usage")]
    operations = [
        migrations.AddField(
            model_name="accountprofile", name="avihub_setup_deferred", field=models.BooleanField(default=False),
        ),
    ]
