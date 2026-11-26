from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0023_profile_photo")]
    operations = [
        migrations.AddField(model_name="pendingregistration", name="language",
                            field=models.CharField(max_length=8, default="pt-br")),
        migrations.AddField(model_name="passwordresetdelivery", name="language",
                            field=models.CharField(max_length=8, default="pt-br")),
        migrations.AddField(model_name="accountprofile", name="language",
                            field=models.CharField(max_length=8, blank=True, default="")),
    ]
