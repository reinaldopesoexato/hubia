from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0028_iaves_subscriptions")]

    operations = [
        migrations.CreateModel(name="CatalogListing", fields=[
            ("link", models.OneToOneField(to="bridge.link", on_delete=models.CASCADE, primary_key=True,
                                         related_name="catalog_listing", serialize=False)),
            ("listed_at", models.DateTimeField()),
            ("tools", models.JSONField(default=list)),
        ]),
    ]
