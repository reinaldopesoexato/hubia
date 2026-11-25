"""Exercita o login com o banco real dentro de uma transação revertida ao final."""
import secrets
from uuid import uuid4

from django.db import connection, transaction
from django.test import Client

from smoke_agents import verify_agents
from create_user import activate_owner, create_account

with transaction.atomic():
    password = secrets.token_urlsafe(32)
    user = create_account("teste-" + secrets.token_hex(12), password)
    other = create_account("outro-" + secrets.token_hex(12), secrets.token_urlsafe(32))
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT FROM hub_identity.users WHERE platform_owner)")
        test_activation = not cursor.fetchone()[0]
        if test_activation:
            activate_owner(user)
        cursor.execute("SET LOCAL ROLE hub_portal")
        cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(other.pk)])
        other_id = cursor.fetchone()[0]
    client = Client(enforce_csrf_checks=True, HTTP_HOST="hubia.psxt.io")
    assert client.get("/conta/", secure=True).status_code == 302
    assert client.get("/conta/entrar/", secure=True).status_code == 200
    csrf = client.cookies["__Host-hubia-csrf"].value
    form = {"username": user.username, "password": password}
    assert client.post("/conta/entrar/", form, secure=True).status_code == 403
    form["csrfmiddlewaretoken"] = csrf
    http = dict(secure=True, HTTP_ORIGIN="https://hubia.psxt.io")
    assert client.post("/conta/entrar/", {**form, "password": "invalid"}, **http).status_code == 200
    assert client.post("/conta/entrar/", form, **http).status_code == 302
    page = client.get("/conta/", secure=True)
    assert page.status_code == 200 and user.username in page.content.decode()
    assert page.content.decode().count("Espaço pessoal") == 1
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM hub_tenancy.memberships WHERE user_id = %s", [other_id])
        assert cursor.fetchone()[0] == 0
    admin_url = "/conta/administracao/"
    ordinary = Client(HTTP_HOST="hubia.psxt.io")
    ordinary.force_login(other)
    assert ordinary.get(admin_url, secure=True).status_code == 403
    assert ordinary.post(admin_url, {"action": "create_org", "name": "Teste"}, **http).status_code == 403
    from bridge.smoke import verify_bridge
    verify_bridge(user,other)
    verify_agents(client,ordinary,user,http)
    for days in (7, 30):
        dashboard = client.get("/conta/", {"dias": days}, secure=True)
        assert dashboard.status_code == 200
        context = dashboard.content.decode()
        assert 'class="bar"' in context and context.count('class="bar"') == days
        assert "Concluída" in context and "Falhou" in context
    assert 'href="/conta/agentes/"' in context
    from bridge.smoke_notifications import verify_notifications
    verify_notifications(client,ordinary,user,http)
    directory = client.get("/conta/agentes/", secure=True)
    assert directory.status_code == 200
    listing = directory.content.decode()
    assert "Espaço pessoal" in listing and "Principal" in listing
    assert "Notificações teste" in listing and "Envia avisos pelos canais habilitados" in listing
    assert "Procedimentos" not in listing  # Agente desativado no teste do espaço.
    assert "Notificações teste" not in ordinary.get("/conta/agentes/", secure=True).content.decode()
    assert Client(HTTP_HOST="hubia.psxt.io").get("/conta/agentes/", secure=True).status_code == 302
    isolated_dashboard = ordinary.get("/conta/", secure=True).content.decode()
    assert "Nenhuma consulta no período" in isolated_dashboard
    assert "Concluída" not in isolated_dashboard
    print("Painel de 7 e 30 dias e isolamento de atividade verificados.")
    if test_activation:
        assert client.get(admin_url, secure=True).status_code == 200
        organization = "Teste " + secrets.token_hex(12)
        csrf = client.cookies["__Host-hubia-csrf"].value

        def manage(action, expected=302, **fields):
            response = client.post(admin_url, {"action": action, "csrfmiddlewaretoken": csrf, **fields}, **http)
            assert response.status_code == expected, (action, response.status_code)

        manage("create_org", name=organization, username=other.username)
        with connection.cursor() as cursor:
            cursor.execute("SELECT tenant_id, workspace_id FROM hub_admin.organization_directory() WHERE organization = %s",
                           [organization])
            tenant, workspace = map(str, cursor.fetchone())
        manage("rename_org", tenant=tenant, name=organization + " A")
        manage("create_space", tenant=tenant, name="Operações")
        with connection.cursor() as cursor:
            cursor.execute("SELECT workspace_id FROM hub_admin.organization_directory() WHERE tenant_id=%s AND workspace=%s", [tenant, "Operações"])
            operations = str(cursor.fetchone()[0])
        manage("rename_space", tenant=tenant, workspace=workspace, name="Matriz")
        manage("grant_space", tenant=tenant, workspace=workspace, username=user.username)
        assert "Matriz" in client.get("/conta/", secure=True).content.decode()
        manage("revoke_space", tenant=tenant, workspace=workspace, username=user.username)
        assert "Matriz" not in client.get("/conta/", secure=True).content.decode()
        manage("suspend", tenant=tenant)
        manage("create_space", expected=400, tenant=tenant, name="Bloqueado")
        manage("activate", tenant=tenant)
        assert "Operações" in client.get(admin_url, secure=True).content.decode()
        from django.contrib.auth import get_user_model
        people_url = "/conta/administracao/usuarios/"
        assert ordinary.get(people_url,secure=True).status_code==404
        assert client.get(people_url,secure=True).status_code==200
        login = "cadastro-" + secrets.token_hex(8)
        create = {"action":"create","username":login,"password1":password,"password2":password,"csrfmiddlewaretoken":csrf}
        assert client.post(people_url,create,**http).status_code==302
        created = get_user_model().objects.get(username=login)
        for action,expected in (("disable",False),("enable",True)):
            assert client.post(people_url,{"action":action,"account":created.pk,"csrfmiddlewaretoken":csrf},**http).status_code==302
            created.refresh_from_db()
            assert created.is_active == expected
        space_url = f"/conta/espacos/{tenant}/{workspace}/"
        assert client.get(space_url,secure=True).status_code==404
        assert client.get(space_url+"gestao/",secure=True).status_code==200
        assert ordinary.get(space_url,secure=True).status_code==200
        grant_url = space_url + "gestao/membros/"
        assert ordinary.post(grant_url,{"action":"grant","username":login},**http).status_code==302
        assert ordinary.post(grant_url,{"action":"role","username":login,"role":"viewer"},**http).status_code==302
        viewer = Client(HTTP_HOST="hubia.psxt.io")
        viewer.force_login(created)
        assert viewer.get(space_url,secure=True).status_code==200
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM hub_tenancy.agents WHERE tenant_id=%s AND workspace_id=%s AND kind='guide'",[tenant,workspace])
            guide_url = space_url + "agentes/" + str(cursor.fetchone()[0]) + "/"
        from bridge.smoke_github import verify_github
        verify_github(ordinary,viewer,tenant,workspace)
        from bridge.smoke_mail import verify_mail
        verify_mail(ordinary,viewer,tenant,workspace,created)
        from bridge.smoke_workspaces import verify_workspace_selection
        verify_workspace_selection(viewer, ordinary, created, tenant, workspace, operations, manage)
        from smoke_spaces import verify_space_lifecycle
        verify_space_lifecycle(client, ordinary, other, tenant, workspace, csrf, http)
        assert viewer.post(guide_url,{"question":"teste","request_id":str(uuid4())},**http).status_code==403
        assert ordinary.post(grant_url,{"action":"revoke","username":login},**http).status_code==302
        assert viewer.get(space_url,secure=True).status_code==404
        print("Concessão, papel de leitor e revogação pelo responsável institucional verificados.")
        print("Cadastro, desativação, reativação, auditoria e limites do administrador verificados.")
        print("Administração de instituições, espaços e permissões verificada.")
    assert client.post("/conta/sair/", {"csrfmiddlewaretoken": client.cookies["__Host-hubia-csrf"].value}, **http).status_code == 302
    assert client.get("/conta/", secure=True).status_code == 302
    transaction.set_rollback(True)
print("Login, autorização, CSRF e saída verificados.")
