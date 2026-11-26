from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0012_link_email_only")]
    operations = [
        # Previous receipts contain a fingerprint and state, but no dispatch time.
        migrations.AddField(model_name="outboundemail", name="created_at", field=models.DateTimeField(null=True)),
        migrations.AddField(model_name="outboundemail", name="recipient", field=models.EmailField(blank=True, default="", max_length=254)),
    ]
