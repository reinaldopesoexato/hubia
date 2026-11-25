from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0011_mailbox_signature")]
    operations = [
        migrations.AddField(model_name="link", name="email_only", field=models.BooleanField(default=False)),
    ]
