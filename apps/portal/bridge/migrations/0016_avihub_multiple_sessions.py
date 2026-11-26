from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0015_avihub_session_lifetime")]
    operations = [
        migrations.AddField(model_name="avihubsession", name="email",
                            field=models.EmailField(blank=True, default="", max_length=254)),
        migrations.AddField(model_name="avihubsession", name="label",
                            field=models.CharField(blank=True, default="", max_length=80)),
        migrations.RemoveConstraint(model_name="avihubsession", name="avihub_personal_session"),
    ]
