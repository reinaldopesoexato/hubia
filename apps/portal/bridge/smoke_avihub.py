from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.urls import reverse
from django.utils import timezone

from . import avihub, mcp
from .models import AviHubAgent, Link


def verify_avihub_session_storage(user):
    # Also exercise upgraded installation schemas, where there is already a platform owner.
    agent = AviHubAgent.objects.create(tenant=uuid4(), origin="https://avihub.example")
    with patch.object(avihub.Panel, "authenticate", side_effect=[True, False]):
        for email in ("matriz@example.org", "filial@example.org"):
            avihub.start_session(agent, user, {"email":email, "password":"fixture"})
    assert set(avihub.personal(agent.tenant, user).values_list("ready", flat=True)) == {True, False}
    agent.delete()


def verify_avihub(manager, viewer, user, tenant, workspace, other, second_space):
    from .agent_catalog import register_connector
    from .github import institution
    from smoke_agents import select_institution_agent

    agent = AviHubAgent.objects.create(tenant=tenant, origin="https://avihub.example")
    with connection.cursor() as cursor:
        institution(cursor,other,tenant)
        register_connector(cursor,tenant,agent.pk,"AviHUB","avihub",avihub.PURPOSE)
    url = reverse("use_agent", args=[tenant, workspace, agent.pk])
    assert viewer.get(url,secure=True).status_code == 404
    assert select_institution_agent(manager, tenant, workspace, agent.pk, True, {"secure":True}).status_code == 302
    empty = viewer.get(url, secure=True).content
    assert b"Contas vinculadas" in empty and b"Incluir conta" in empty and b'name="password"' not in empty
    assert b'name="password"' in viewer.get(url + "?nova=1", secure=True).content
    link = Link(user=user, tenant=tenant, space=workspace, protocol="mcp")

    def offered():
        return mcp.EQUIPMENT in mcp.available_tools(link, {}, False)

    params = {"name": "consultar_equipamento", "arguments": {"serie": "3434-2023", "modelo": "SAC"}}

    def check_link_required(*, pending=False):
        assert offered()
        with patch.object(avihub.api, "equipment_query") as lookup:
            for probe in (params, {"name": avihub.api.ACCOUNTS, "arguments": {}}):
                result = mcp.call_tool(link, {}, False, probe)
                assert result["estado"] == "vinculo_necessario"
                assert result["url_vinculo"] == mcp.ORIGIN + url + ("" if pending else "?nova=1")
                assert "consultar operações do AviHUB" in result["mensagem"]
            lookup.assert_not_called()

    check_link_required()
    with patch.object(avihub.Panel, "authenticate", return_value=False):
        response = viewer.post(url, {"action":"login","email":"person@example.org","password":"fixture"}, secure=True)
        assert response.status_code == 302
    check_link_required(pending=True)
    pending = avihub.personal(tenant, user).get()
    assert timezone.now() < pending.expires <= timezone.now() + timedelta(minutes=5)
    with patch.object(avihub.timezone, "now", return_value=pending.expires):
        assert not avihub.personal(tenant, user).exists()
    units = [{"id": "1", "nome": "Matrizes"}, {"id": "2", "nome": "Recria"}]
    with patch.object(avihub.Panel, "units", return_value=units), patch.object(avihub.Panel, "authenticate", return_value=True):
        viewer.post(url, {"action":"verify","code":"fixture", "conta":pending.account_ref}, secure=True)
        assert avihub.personal(tenant, user).get().expires is None
        with patch.object(avihub.timezone, "now", return_value=timezone.now() + timedelta(days=365)):
            assert offered()
        assert not avihub.personal(tenant, other).exists()
        assert not avihub.personal(uuid4(), user).exists()
        link.email_only = True
        assert not offered()
        link.email_only = False
        with patch.object(avihub.Panel, "home"), patch.object(avihub.Panel, "require_context"), patch.object(
                avihub.api, "equipment_query", return_value={"serie": "3434-2023"}) as lookup:
            prompt = mcp.call_tool(link, {}, False, params)
            assert prompt["estado"] == "escolher_unidade" and prompt["unidades"] == units
            lookup.assert_not_called()
            params["arguments"]["unidade"] = "Recria"
            assert mcp.call_tool(link, {}, False, params)["serie"] == "3434-2023"
            assert lookup.call_args.args[1:3] == (units[1], "consultar_equipamento")
    with patch.object(avihub.Panel, "authenticate", return_value=False):
        response = viewer.post(url, {"action":"login", "apelido":"Filial", "email":"filial@example.org",
                                    "password":"fixture"}, secure=True)
    assert response.status_code == 302
    second = avihub.personal(tenant, user).get(ready=False)
    assert second.pk != pending.pk
    assert second.account_ref in response.url
    assert avihub.personal(tenant, user).filter(ready=True, pk=pending.pk).exists()
    account_list = {"name": avihub.api.ACCOUNTS, "arguments": {}}
    assert [a["id"] for a in mcp.call_tool(link, {}, False, account_list)["contas"]] == [pending.account_ref]
    with patch.object(avihub.Panel, "authenticate", return_value=True):
        viewer.post(url, {"action":"verify", "code":"fixture", "conta":second.account_ref}, secure=True)
    assert len(mcp.call_tool(link, {}, False, account_list)["contas"]) == 2
    with patch.object(avihub, "restore") as restore:
        prompt = mcp.call_tool(link, {}, False, params)
        assert prompt["estado"] == "escolher_conta"
        restore.assert_not_called()
        assert viewer.get(url, secure=True).status_code == 200
        restore.assert_not_called()
    with patch.object(avihub.api, "unit_query", return_value={"serie":"3434-2023"}), patch.object(
            avihub, "restore", wraps=avihub.restore) as restored:
        params["arguments"]["conta"] = "Filial"
        result = mcp.call_tool(link, {}, False, params)
        assert result["conta"]["id"] == second.account_ref and restored.call_args.args[0].pk == second.pk
    with patch.object(avihub, "restore") as restore:
        viewer.post(url, {"action":"rename", "apelido":"Matriz", "conta":pending.account_ref}, secure=True)
        response = viewer.get(url + "?conta=" + pending.account_ref, secure=True)
        assert response.status_code == 200 and b"Matriz" in response.content
        assert b"Consultar equipamento" not in response.content
        listing = viewer.get(url, secure=True).content
        assert listing.count(b'name="contas" value=') == 2 and listing.count(b">Editar</a>") == 2
        for action in ("lookup", "activate"):
            viewer.post(url, {"action":action, "conta":pending.account_ref}, secure=True)
        restore.assert_not_called()
    viewer.post(url, {"action":"delete"}, secure=True)
    assert avihub.personal(tenant, user).count() == 2
    with patch.object(avihub.api, "unit_query", side_effect=avihub.SessionExpired("fixture")):
        expired = mcp.call_tool(link, {}, False, params)
        assert expired["url_vinculo"] == mcp.ORIGIN + url + "?nova=1" and "expirou" in expired["mensagem"]
    assert avihub.personal(tenant, user).get().pk == pending.pk
    try:
        mcp.call_tool(link, {}, False, params)
    except avihub.api.AccessDenied:
        pass
    else:
        raise AssertionError("Uma conta removida não pode cair na outra sessão.")
    with patch.object(avihub.Panel, "authenticate", return_value=True):
        viewer.post(url, {"action":"login", "email":"another@example.org", "password":"fixture"}, secure=True)
    additional = avihub.personal(tenant, user).exclude(pk=pending.pk).get()
    foreign = avihub.AviHubSession.objects.create(agent=agent, user=other, ready=True, encrypted=b"fixture")
    for ref in (foreign.account_ref, "conta-99999999999999999999", second.account_ref):
        viewer.post(url, {"action":"delete", "contas":[pending.account_ref, ref]}, secure=True)
        assert avihub.personal(tenant, user).count() == 2
    refs = [pending.account_ref, additional.account_ref]
    preview = viewer.post(url, {"action":"review_delete", "contas":refs}, secure=True)
    assert b"Confirmar exclus" in preview.content and all(ref.encode() in preview.content for ref in refs)
    assert avihub.personal(tenant, user).count() == 2
    response = viewer.post(url, {"action":"delete", "contas":refs}, secure=True)
    assert response.status_code == 302 and not avihub.personal(tenant, user).exists()
    assert avihub.personal(tenant, other).filter(pk=foreign.pk).exists()
    check_link_required()
    from uuid import UUID
    selected = {UUID(str(workspace)):"Matriz",UUID(str(second_space)):"Operações"}
    try:
        mcp.call_tool(link, {"spaces":selected}, False, {"name":avihub.api.ACCOUNTS,"arguments":{"space_id":str(second_space)}})
    except ValueError:
        pass
    else:
        raise AssertionError("AviHUB desmarcado no espaço escolhido")
    assert select_institution_agent(manager, tenant, workspace, agent.pk, False, {"secure":True}).status_code == 302
    assert viewer.get(url,secure=True).status_code == 404
    assert not offered()
    agent.delete()
    print("AviHUB: autenticação pessoal, MFA, catálogo MCP, isolamento e desconexão verificados com painel simulado.")
