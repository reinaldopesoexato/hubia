from uuid import uuid4

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL), ("bridge", "0029_mcp_catalog_tools")]

    operations = [
        migrations.CreateModel(name="AviHubReport", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid4, editable=False, serialize=False)),
            ("user", models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.CASCADE)),
            ("tenant", models.UUIDField()),
            ("space", models.UUIDField()),
            ("agent", models.ForeignKey(to="bridge.avihubagent", on_delete=models.CASCADE)),
            ("session", models.ForeignKey(to="bridge.avihubsession", on_delete=models.SET_NULL, null=True)),
            ("link", models.ForeignKey(to="bridge.link", on_delete=models.SET_NULL, null=True)),
            ("from_mcp", models.BooleanField(default=False)),
            ("request_id", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("parameters", models.JSONField(default=dict)),
            ("native_id", models.CharField(max_length=20, blank=True)),
            ("channel", models.CharField(max_length=16, default="painel")),
            ("destination", models.CharField(max_length=254, blank=True)),
            ("state", models.CharField(max_length=9, default="pending", choices=[
                ("pending", "Relatório na fila"), ("running", "Gerando no AviHUB"), ("ready", "Relatório pronto"),
                ("failed", "Relatório não gerado"), ("uncertain", "Geração sem confirmação"),
            ])),
            ("delivery", models.CharField(max_length=9, default="none", choices=[
                ("none", "Disponível no painel"), ("pending", "Aviso na fila"), ("accepted", "Aviso aceito pelo canal"),
                ("failed", "Aviso recusado pelo canal"), ("skipped", "Aviso não enviado"),
                ("uncertain", "Aviso sem confirmação"),
            ])),
            ("error", models.CharField(max_length=300, blank=True)),
            ("created", models.DateTimeField(auto_now_add=True)),
            ("started_at", models.DateTimeField(null=True)),
        ], options={
            "constraints": [models.UniqueConstraint(fields=["user", "request_id"], name="avihub_report_request_once")],
            "indexes": [models.Index(fields=["state", "created"], name="avihub_report_queue"),
                        models.Index(fields=["delivery", "created"], name="avihub_report_delivery")],
        }),
    ]
