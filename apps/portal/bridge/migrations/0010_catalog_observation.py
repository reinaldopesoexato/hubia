from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0009_outbound_email"), ("oauth2_provider", "__first__")]
    operations = [
        migrations.CreateModel(name="CatalogObservation", fields=[
            ("token", models.OneToOneField(to="oauth2_provider.accesstoken", primary_key=True,
                                          serialize=False, on_delete=models.CASCADE)),
            ("link", models.ForeignKey(to="bridge.link", on_delete=models.CASCADE)),
            ("revision", models.CharField(max_length=64, blank=True)),
            ("listed_at", models.DateTimeField(null=True)),
            ("announced", models.CharField(max_length=64, blank=True)),
            ("announced_at", models.DateTimeField(null=True)),
        ])
    ]
