import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bridge", "0003_github_connection")]
    operations = [
        migrations.CreateModel(name="NoteLibrary", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("name", models.CharField(max_length=120)),
            ("repository_id", models.BigIntegerField()),
            ("connection", models.OneToOneField(on_delete=models.CASCADE, to="bridge.githubconnection")),
        ]),
        migrations.CreateModel(name="LibraryAccess", fields=[
            ("id", models.BigAutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
            ("space", models.UUIDField()),
            ("folders", models.JSONField(default=list)),
            ("library", models.ForeignKey(on_delete=models.CASCADE, to="bridge.notelibrary")),
        ], options={"constraints":[models.UniqueConstraint(fields=["library","space"], name="library_space_once")]}),
    ]
