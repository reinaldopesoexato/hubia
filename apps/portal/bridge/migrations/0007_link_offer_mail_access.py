from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0006_mailbox")]
    operations = [
        # Existing connections keep their previous mailbox permissions on reauthorization.
        migrations.AddField(model_name="link", name="offer_mail_access",
                            field=models.BooleanField(default=False), preserve_default=False),
        migrations.AlterField(model_name="link", name="offer_mail_access",
                              field=models.BooleanField(default=True)),
    ]
