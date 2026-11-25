from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0008_workspace_selection")]
    operations = [
        migrations.AddField(
            model_name="mailbox", name="send_links",
            field=models.ManyToManyField(blank=True, related_name="outbound_mailboxes", to="bridge.link"),
        ),
        migrations.CreateModel(name="OutboundEmail", fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
            ("request_id", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("state", models.CharField(default="registrado", max_length=16)),
            ("link", models.ForeignKey(to="bridge.link", on_delete=models.CASCADE)),
        ], options={"constraints":[models.UniqueConstraint(fields=["link", "request_id"], name="mail_send_per_link")]}),
    ]
