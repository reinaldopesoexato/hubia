from django.db import migrations


def preserve_individual_grants(apps, schema_editor):
    # The old Gemini picker stored single-space consent in Link.space only.
    # Its all-spaces option already wrote a WorkspaceSelection row.
    db = schema_editor.connection.alias
    Application = apps.get_model("oauth2_provider", "Application")
    Link = apps.get_model("bridge", "Link")
    Selection = apps.get_model("bridge", "WorkspaceSelection")
    clients = Application.objects.using(db).filter(user__isnull=True, name="Gemini · HUB IA")
    links = Link.objects.using(db).filter(protocol="mcp", client_id__in=clients.values("client_id"),
                                         workspaceselection__isnull=True)
    for link_id, space in links.values_list("pk", "space").iterator():
        Selection.objects.using(db).get_or_create(link_id=link_id, defaults={"spaces": [str(space)]})


class Migration(migrations.Migration):
    dependencies = [("bridge", "0019_client_accounts")]
    operations = [migrations.RunPython(preserve_individual_grants, migrations.RunPython.noop)]
