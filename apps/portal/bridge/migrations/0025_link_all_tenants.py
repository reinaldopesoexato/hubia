from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0024_account_language")]
    operations = [
        migrations.AddField(model_name="link", name="all_tenants", field=models.BooleanField(default=False, db_default=False)),
    ]
