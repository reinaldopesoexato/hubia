from uuid import uuid4

from django.db import connection
from django.urls import reverse

from bridge.models import CatalogAlerts, LibraryAccess, NoteLibrary
from bridge.portal import create_connection


def verify_space_lifecycle(owner, member, account, tenant, principal, csrf, http):
    principal_url = reverse("workspace_settings", args=[tenant,principal])
    def update(url, action, name=""):
        return owner.post(url, {"csrfmiddlewaretoken":csrf,"action":action,"name":name}, **http)

    assert update(principal_url, "trash_space", "Matriz").status_code == 400
    with connection.cursor() as cursor:
        cursor.execute("""SELECT workspace_id FROM hub_admin.workspace_inventory()
                          WHERE tenant_id=%s AND workspace='Operações'""", [tenant])
        space = cursor.fetchone()[0]
    settings = reverse("workspace_settings", args=[tenant,space])
    home = reverse("workspace", args=[tenant,space])
    assert member.post(settings,{"action":"trash_space","name":"Operações"},**http).status_code == 404
    assert owner.get(reverse("workspace_settings",args=[uuid4(),space]),**http).status_code == 404
    assert update(settings, "rename_space","Operações QA").status_code == 302
    assert update(settings,"trash_space","Operações").status_code == 400

    library = NoteLibrary.objects.get(connection_id=tenant)
    LibraryAccess.objects.create(library=library, space=space, folders=["Manuais"])
    CatalogAlerts.objects.create(tenant=tenant, space=space)
    link, token = create_connection(account, tenant, space, {"protocol":"a2a","name":"QA Espaço","writable":False})
    rpc = {"jsonrpc":"2.0","id":"qa","method":"SendMessage","params":{"message":{"messageId":"qa",
           "role":"ROLE_USER","parts":[{"text":"listar agentes"}]}}}
    def catalog_status():
        return member.post("/conta/a2a",rpc,content_type="application/json",secure=True,HTTP_AUTHORIZATION="Bearer "+token).status_code
    assert catalog_status() == 200
    assert "Manuais" in owner.get(settings,**http).content.decode()
    assert update(settings,"trash_space","Operações QA").status_code == 302
    assert member.get(home,**http).status_code == 404
    assert owner.get(reverse("manage_agents",args=[tenant,space]),**http).status_code == 404
    assert "Operações QA" not in member.get("/conta/",**http).content.decode()
    assert "Lixeira de espaços" in owner.get(reverse("administration"),**http).content.decode()
    assert catalog_status() in (401,403)
    assert member.get(reverse("workspace",args=[tenant,principal]),**http).status_code == 200
    assert update(settings,"rename_space","Indevido").status_code == 400
    assert update(settings,"restore_space").status_code == 302
    assert "Operações QA" in member.get(home,**http).content.decode()
    assert catalog_status() == 200
    assert LibraryAccess.objects.get(library=library,space=space).folders == ["Manuais"]
    assert CatalogAlerts.objects.filter(tenant=tenant,space=space).exists()
    print("Espaços: nome, lixeira, restauração, permissões, A2A, biblioteca e isolamento verificados.")
