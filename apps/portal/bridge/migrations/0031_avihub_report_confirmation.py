from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0030_avihub_reports")]

    operations = [
        migrations.AddField("avihubreport", "native_notification_id", models.CharField(max_length=20, blank=True, default="")),
        migrations.AddField("avihubreport", "file_name", models.CharField(max_length=255, blank=True, default="")),
        migrations.AddField("avihubreport", "file_type", models.CharField(max_length=150, blank=True, default="")),
        migrations.AddField("avihubreport", "file_size", models.PositiveIntegerField(null=True)),
        migrations.AddField("avihubreport", "file_from_preview", models.BooleanField(default=False)),
        migrations.AddField("avihubreport", "printed_pdf", models.BinaryField(null=True, editable=False)),
        migrations.AddField("avihubreport", "next_check_at", models.DateTimeField(null=True)),
        migrations.AlterField("avihubreport", "state", models.CharField(max_length=9, default="pending", choices=[
            ("pending", "Relatório na fila"), ("running", "Gerando no AviHUB"),
            ("waiting", "Aguardando a notificação do AviHUB"), ("ready", "Relatório pronto"),
            ("preview", "Prévia disponível no AviHUB"), ("failed", "Relatório não gerado"),
            ("uncertain", "Geração sem confirmação"),
        ])),
        migrations.AddIndex("avihubreport", models.Index(fields=["state", "next_check_at"], name="avihub_report_notification")),
    ]
