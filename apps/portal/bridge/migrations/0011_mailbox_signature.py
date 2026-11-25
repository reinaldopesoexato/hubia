from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0010_catalog_observation")]
    operations = [
        migrations.AddField(model_name="mailbox", name="signature_html",
                            field=models.TextField(blank=True, default="")),
    ]
