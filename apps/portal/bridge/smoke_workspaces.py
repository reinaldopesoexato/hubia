import json
from uuid import uuid4

from django.urls import reverse
from oauth2_provider.models import get_access_token_model

from .models import Link, WorkspaceSelection


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
        return {row["space_id"] for row in data}
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
    assert save("selected", [origin]) == 302 and ids() == {origin}
    assert save("selected") == 302 and call("list_agents").status_code == 401
    assert save("selected", [origin]) == 302 and ids() == {origin}
    peer = Link.objects.create(user=user, tenant=tenant, space=origin, protocol="mcp",
                               name="Segunda IA", client_id=str(uuid4()))
    page = viewer.get(url, secure=True).content.decode()
    assert page.count(">Salvar permissões</button>") == 1 and f"mode-{peer.pk}" in page
    def batch(first_mode, peer_mode, first_spaces=(), peer_spaces=(), peer_initial=origin):
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
