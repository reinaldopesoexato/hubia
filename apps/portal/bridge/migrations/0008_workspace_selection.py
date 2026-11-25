from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0007_link_offer_mail_access")]
    operations = [
        migrations.CreateModel(name="WorkspaceSelection", fields=[
            ("link", models.OneToOneField(to="bridge.link", on_delete=models.CASCADE, primary_key=True, serialize=False)),
            ("spaces", models.JSONField(null=True)),
        ])
    ]
