import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bridge", "0027_iaves_feedback"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IAvesSubscription",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("user", models.ForeignKey(on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL)),
                ("space", models.UUIDField()),
                ("session", models.ForeignKey(null=True, on_delete=models.SET_NULL, to="bridge.avihubsession")),
                ("kind", models.CharField(max_length=8, choices=[("summary", "Resumo de pesagens do lote"),
                                                                  ("offline", "Equipamentos sem comunicação")])),
                ("frequency", models.CharField(max_length=6, choices=[("daily", "Diária"), ("weekly", "Semanal")], default="weekly")),
                ("delivery_email", models.EmailField(max_length=254, blank=True, default="")),
                ("unit", models.CharField(max_length=18)),
                ("unit_label", models.CharField(max_length=160)),
                ("nucleus", models.CharField(max_length=18, blank=True)),
                ("batch", models.CharField(max_length=18, blank=True)),
                ("batch_label", models.CharField(max_length=160, blank=True)),
                ("active", models.BooleanField(default=True)),
                ("next_due", models.DateTimeField()),
                ("status", models.CharField(max_length=11, default="waiting", choices=[
                    ("waiting", "Aguardando primeira consulta"), ("ready", "Consultado"),
                    ("blocked", "Confira o vínculo, a unidade ativa e as permissões no AviHUB"),
                    ("unavailable", "A consulta ao AviHUB falhou. O painel tentará na próxima verificação")])),
                ("offline_ids", models.JSONField(default=list)),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=["user", "space", "session", "unit", "kind", "nucleus", "batch"],
                                                        name="iaves_subscription_target_once")],
                "indexes": [models.Index(fields=["active", "next_due"], name="iaves_subscription_due")],
            },
        ),
        migrations.CreateModel(
            name="IAvesNotice",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("subscription", models.ForeignKey(on_delete=models.CASCADE, to="bridge.iavessubscription")),
                ("scheduled_for", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("title", models.CharField(max_length=120)),
                ("body", models.TextField()),
                ("mail_status", models.CharField(max_length=9, db_index=True, default="portal", choices=[
                    ("portal", "No painel"), ("pending", "E-mail em fila"),
                    ("accepted", "Aceito pelo servidor de e-mail"), ("uncertain", "Envio de e-mail sem confirmação"),
                    ("skipped", "E-mail não enviado. Confira a conta e o canal")])),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=["subscription", "scheduled_for"], name="iaves_notice_window_once")],
            },
        ),
    ]
