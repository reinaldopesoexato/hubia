from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("bridge", "0001_initial")]
    operations = [
        migrations.CreateModel(name="NotificationChannel", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("agent", models.UUIDField()), ("tenant", models.UUIDField()), ("space", models.UUIDField()),
            ("channel", models.CharField(max_length=24)), ("enabled", models.BooleanField(default=False)),
            ("encrypted", models.BinaryField()),
        ], options={"constraints": [models.UniqueConstraint(fields=["tenant","space","agent","channel"],name="notification_channel_once")]}),
        migrations.CreateModel(name="NotificationDestination", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=100)), ("address", models.CharField(max_length=128)),
            ("sector", models.UUIDField(null=True)),
            ("channel", models.ForeignKey(to="bridge.notificationchannel", on_delete=django.db.models.deletion.CASCADE)),
        ], options={"constraints": [models.UniqueConstraint(fields=["channel","name"],name="notification_destination_name")]}),
    ]
