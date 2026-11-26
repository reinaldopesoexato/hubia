import json
from datetime import timedelta
from uuid import uuid4

from django.db import connection
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model
from portal_identity import bind_account

from .models import CatalogListing, Link, WorkspaceSelection
from .workspaces import visible


def verify_workspace_selection(viewer, outsider, user, tenant, origin, second, manage):
    url = reverse("ai_workspaces")
    link = Link.objects.get(user=user, tenant=tenant, name="Gemini")
    token = get_access_token_model().objects.get(application__client_id=link.client_id, user=user)
    def call(name, **arguments):
        return viewer.post("/conta/mcp", {"jsonrpc":"2.0","id":1,"method":"tools/call",
                           "params":{"name":name,"arguments":arguments}}, content_type="application/json",
                           secure=True, HTTP_AUTHORIZATION="Bearer " + token.token)
    def ids():
        data = json.loads(call("listar_espacos").json()["result"]["content"][0]["text"])
        expected = {row["space_id"] for row in data}
        guide = json.loads(call("consultar_guia").json()["result"]["content"][0]["text"])
        assert {group["space"] for group in guide} == expected
        return expected
    def discovered_spaces():
        response = viewer.post("/conta/mcp", {"jsonrpc":"2.0","id":1,"method":"tools/list"},
                               content_type="application/json", secure=True, HTTP_AUTHORIZATION="Bearer " + token.token)
        tool = next(t for t in response.json()["result"]["tools"] if t["name"] == "list_agents")
        return {a["space_id"] for a in tool["_meta"]["io.psxt.hubia/agents"]}
    def save(mode, spaces=()):
        return viewer.post(url, {"link":link.pk,"mode":mode,"spaces":list(spaces)}, secure=True).status_code

    assert viewer.get(url, secure=True).status_code == 200
    assert outsider.post(url, {"link":link.pk,"mode":"all"}, secure=True).status_code == 404
    assert ids() == {origin} and save("selected", [second]) == 400
    assert save("all") == 302 and ids() == discovered_spaces() == {origin}
    manage("grant_space", tenant=tenant, workspace=second, username=user.username)
    assert ids() == discovered_spaces() == {origin, second}
    agents = json.loads(call("list_agents").json()["result"]["content"][0]["text"])
    assert {a["space_id"] for a in agents} == {origin, second}
    assert save("selected", [second]) == 302 and ids() == discovered_spaces() == {second}
    assert "error" in call("list_tasks").json()
    assert save("selected", [str(uuid4())]) == 400 and ids() == {second}
    manage("revoke_space", tenant=tenant, workspace=second, username=user.username)
    assert call("listar_espacos").status_code == 401
    revoked_page = viewer.get(url, secure=True).content.decode()
    assert "A conexão perdeu acesso aos espaços escolhidos." in revoked_page
    assert "Ferramentas do último catálogo" in revoked_page
    assert save("selected", [origin]) == 302 and ids() == {origin}
    assert save("selected") == 302 and call("list_agents").status_code == 401
    assert save("selected", [origin]) == 302 and ids() == {origin}
    peer = Link.objects.create(user=user, tenant=tenant, space=origin, protocol="mcp",
                               name="Segunda IA", client_id=str(uuid4()))
    page = viewer.get(url, secure=True).content.decode()
    assert page.count(">Salvar permissões</button>") == 1 and f"mode-{peer.pk}" in page
    assert f'name="initial-{peer.pk}" value="all"' in page
    def batch(first_mode, peer_mode, first_spaces=(), peer_spaces=(), peer_initial="all"):
        return viewer.post(url, {
            "link": [str(link.pk), str(peer.pk)],
            f"mode-{link.pk}": first_mode, f"initial-{link.pk}": origin,
            f"spaces-{link.pk}": list(first_spaces),
            f"mode-{peer.pk}": peer_mode, f"initial-{peer.pk}": peer_initial,
            f"spaces-{peer.pk}": list(peer_spaces),
        }, secure=True).status_code

    assert batch("all", "selected", peer_spaces=[str(uuid4())]) == 400
    assert WorkspaceSelection.objects.get(link=link).spaces == [origin]
    assert not WorkspaceSelection.objects.filter(link=peer).exists()
    assert batch("all", "paused") == 302
    assert WorkspaceSelection.objects.get(link=link).spaces is None
    assert WorkspaceSelection.objects.get(link=peer).spaces == []
    assert batch("paused", "paused", peer_initial="") == 409
    assert batch("selected", "selected", [origin], [origin], peer_initial="") == 302
    assert WorkspaceSelection.objects.get(link=link).spaces is None
    assert WorkspaceSelection.objects.get(link=peer).spaces == [origin]
    print("Espaços das IAs: botão único, salvamento em lote, conflito, isolamento, revogação e pausa verificados.")

    verify_unified_workspace_selection(viewer, outsider, user, origin, second)


def verify_unified_workspace_selection(client, outsider, user, institutional_space, revoked_space):
    with connection.cursor() as cursor:
        bind_account(cursor, user)
        cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships WHERE role='personal_owner'")
        personal_tenant, = cursor.fetchone()
        personal_space = visible(cursor, personal_tenant)[0][0]

    application = get_application_model().objects.create(name="Conexão unificada de teste", client_type="public",
                                                          authorization_grant_type="authorization-code")
    link = Link.objects.create(user=user, tenant=personal_tenant, space=personal_space,
                               name="Conexão unificada de teste", protocol="mcp", client_id=application.client_id,
                               writable=True, all_tenants=True)
    url = reverse("ai_workspaces")

    def create_token(scope):
        return get_access_token_model().objects.create(
            user=user, application=application, token=str(uuid4()), scope=scope,
            expires=timezone.now() + timedelta(minutes=10),
        )

    token = create_token("hub.read")

    def tools(scope="tools/list", name=None):
        message = {"jsonrpc": "2.0", "id": 1, "method": scope}
        if name:
            message["params"] = {"name": name, "arguments": {}}
        return client.post("/conta/mcp", message, content_type="application/json", secure=True,
                           HTTP_AUTHORIZATION="Bearer " + token.token)

    def returned_spaces():
        response = tools("tools/call", "listar_espacos")
        data = json.loads(response.json()["result"]["content"][0]["text"])
        return {row["space_id"] for row in data}

    personal_space = str(personal_space)
    assert returned_spaces() == {personal_space, institutional_space}
    page = client.get(url, secure=True).content.decode()
    assert "Conta pessoal e instituições" in page
    assert "Aguardando a primeira consulta ao catálogo MCP." in page
    assert "escopo hub.reply para responder tarefas" in page
    assert "Conexão unificada de teste" not in outsider.get(url, secure=True).content.decode()
    assert client.post(url, {"link": link.pk, "mode": "selected", "spaces": [revoked_space]}, secure=True).status_code == 400

    assert client.post(url, {"link": link.pk, "mode": "selected", "spaces": [institutional_space]}, secure=True).status_code == 302
    assert returned_spaces() == {institutional_space}
    returned = [tool["name"] for tool in tools().json()["result"]["tools"]]
    visible_tools = set(returned)
    assert "list_tasks" not in visible_tools and "reply_task" not in visible_tools
    assert "error" in tools("tools/call", "list_tasks").json()
    listing = CatalogListing.objects.get(link=link)
    assert listing.tools == returned
    page = client.get(url, secure=True).content.decode()
    assert "Última consulta ao catálogo MCP em" in page and "Ferramentas do último catálogo" in page
    assert all(f"<code>{name}</code>" in page for name in returned)
    assert "Inclua o espaço de origem para disponibilizar as ferramentas de tarefas." in page
    assert token.token not in page
    token.delete()
    page = client.get(url, secure=True).content.decode()
    assert "Reconecte o HUB IA no seu cliente de IA para renovar a autorização OAuth." in page
    assert CatalogListing.objects.get(link=link).tools == returned
    token = create_token("hub.read hub.reply")

    assert client.post(url, {"link": link.pk, "mode": "paused"}, secure=True).status_code == 302
    assert tools().status_code == 401
    page = client.get(url, secure=True).content.decode()
    assert "Selecione espaços e salve para retomar o acesso desta conexão." in page
    assert "Ferramentas do último catálogo indisponíveis agora" in page
    assert client.post(url, {"link": link.pk, "mode": "all"}, secure=True).status_code == 302
    assert returned_spaces() == {personal_space, institutional_space}
    print("MCP unificado: conta pessoal e instituição, seleção, tarefas na origem, pausa, retomada e diagnóstico verificados.")
