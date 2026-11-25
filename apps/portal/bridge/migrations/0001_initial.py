from uuid import uuid4
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name="Link",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid4, editable=False)),
                ("user", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
                ("tenant", models.UUIDField()),
                ("space", models.UUIDField()),
                ("name", models.CharField(max_length=100)),
                ("protocol", models.CharField(max_length=8)),
                ("client_id", models.CharField(max_length=128, db_index=True)),
                ("key_digest", models.CharField(max_length=64, blank=True)),
                ("peer_config", models.BinaryField(null=True)),
                ("writable", models.BooleanField(default=False)),
            ],
            options={"constraints":[models.UniqueConstraint(fields=["client_id","user"],name="bridge_user_client")]},
        ),
        migrations.CreateModel(
            name="Ticket",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid4, editable=False)),
                ("link", models.ForeignKey("bridge.Link", on_delete=models.CASCADE)),
                ("request_id", models.CharField(max_length=128)),
                ("remote_id", models.CharField(max_length=256,blank=True)),
                ("question", models.TextField()),
                ("answer", models.TextField(default='')),
                ("state", models.CharField(max_length=12, default='pending')),
                ("created", models.DateTimeField(auto_now_add=True)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=['link', 'request_id'], name='bridge_request_once')]},
        ),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                ("id", models.CharField(max_length=64, primary_key=True)),
                ("link", models.ForeignKey("bridge.Link", on_delete=models.CASCADE)),
                ("encrypted", models.BinaryField()),
                ("expires", models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name="Delivery",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid4, editable=False)),
                ("ticket", models.ForeignKey("bridge.Ticket", on_delete=models.CASCADE)),
                ("subscription", models.ForeignKey("bridge.Subscription", on_delete=models.CASCADE)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("due", models.DateTimeField()),
                ("state", models.CharField(max_length=12, default='pending')),
            ],
            options={"constraints": [models.UniqueConstraint(fields=['ticket', 'subscription'], name='bridge_delivery_once')]},
        ),
    ]
