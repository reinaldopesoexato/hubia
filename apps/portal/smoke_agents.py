"""Executa o fluxo do espaço usando somente identidades de teste, na transação do deploy."""
from uuid import uuid4
from unittest.mock import patch
from django.db import connection
from django.urls import reverse

from portal_identity import bind_account


def verify_agents(client, ordinary, user, http):
    with connection.cursor() as cursor:
        bind_account(cursor,user)
        cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships WHERE role='personal_owner'")
        tenant = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)",[str(tenant)])
        cursor.execute("SELECT id FROM hub_tenancy.workspaces WHERE tenant_id=%s",[tenant])
        space = cursor.fetchone()[0]
    location = reverse("workspace",args=[tenant,space])
    manage = reverse("manage_agents",args=[tenant,space])
    assert location in client.get("/conta/espacos/",secure=True).content.decode()
    assert client.get(location,secure=True).status_code==200
    assert ordinary.get(location,secure=True).status_code==404
    assert ordinary.get(manage,secure=True).status_code==404
    directory = client.get(manage, secure=True).content.decode()
    assert "?novo=1" in directory and 'id="agent-editor"' not in directory
    creation = client.get(manage + "?novo=1", secure=True)
    assert creation.status_code == 200 and b'name="connector_id"' in creation.content
    csrf = client.cookies["__Host-hubia-csrf"].value
    form = {"kind":"knowledge","name":"Procedimentos","configuration":"Atendimento das 8 às 18 horas.","active":"on","csrfmiddlewaretoken":csrf}
    assert client.post(manage,form,**http).status_code==302
    saved = client.get(manage, secure=True).content.decode()
    assert 'role="status"' in saved and form["name"] in saved
    assert client.post(manage,form,**http).status_code==400
    assert client.post(manage,{**form,"kind":"external","name":"Link","configuration":"javascript:alert(1)"},**http).status_code==400
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM hub_tenancy.agents WHERE tenant_id=%s AND workspace_id=%s AND name=%s",[tenant,space,form["name"]])
        agent = cursor.fetchone()[0]
    url = reverse("use_agent",args=[tenant,space,agent])
    assert client.get(url,secure=True).status_code==200
    assert ordinary.get(url,secure=True).status_code==404
    query = {"question":"Qual o horário de atendimento?","request_id":str(uuid4()),"csrfmiddlewaretoken":csrf}
    assert client.post(url,{k:v for k,v in query.items() if k!="csrfmiddlewaretoken"},**http).status_code==403
    assert ordinary.post(url,query,**http).status_code==404
    assert client.post(url,query,**http).status_code==302
    assert client.post(url,query,**http).status_code==302
    page = client.get(url,secure=True).content.decode()
    assert page.count(form["configuration"])==1
    edit = reverse("edit_agent",args=[tenant,space,agent])
    editor = client.get(edit, secure=True).content.decode()
    assert form["configuration"] in editor and "Membros com acesso" not in editor
    assert edit in client.get(url, secure=True).content.decode()
    assert ordinary.get(edit, secure=True).status_code == 404
    bad = client.post(edit, {**form, "kind":"external", "external_url":"endereço inválido"}, **http)
    assert bad.status_code == 400 and "endereço inválido" in bad.content.decode()
    revised = {**form, "knowledge_text":"Pesagem diária", "external_url":"https://example.org"}
    assert client.post(edit, revised, **http).status_code == 302
    assert "Pesagem diária" in client.get(edit, secure=True).content.decode()
    assert client.post(edit,{k:v for k,v in form.items() if k!="active"},**http).status_code==302
    assert client.get(url,secure=True).status_code==404
    assert client.get(manage,secure=True).status_code==200
    model_url = reverse("configure_model",args=[tenant,space])
    assert ordinary.get(model_url,secure=True).status_code==404
    model_data = {"model":"modelo-de-teste","api_key":"chave-ficticia-do-teste","install_agent":"on","csrfmiddlewaretoken":csrf}
    assert client.post(model_url,model_data,**http).status_code==302
    assert model_data["api_key"] not in client.get(model_url,secure=True).content.decode()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM hub_tenancy.agents WHERE tenant_id=%s AND workspace_id=%s AND configuration='modelo-do-espaco'",[tenant,space])
        chat = reverse("use_agent",args=[tenant,space,cursor.fetchone()[0]])
    with patch("space_views.query_connector",return_value="Resposta de fixture") as call:
        for number in (1,2):
            message = {"question":f"Pergunta {number}","request_id":str(uuid4()),"csrfmiddlewaretoken":csrf}
            assert client.post(chat,message,**http).status_code==302
        cfg, context, _, _, history = call.call_args.args
        assert cfg["token"]==model_data["api_key"] and context["workspace_id"]==str(space)
        assert history==[("Pergunta 1","Resposta de fixture")]
    assert client.post(model_url,{"action":"disconnect","csrfmiddlewaretoken":csrf},**http).status_code==302
    assert client.post(chat,{**message,"request_id":str(uuid4())},**http).status_code==302
    assert "Conector indisponível" in client.get(chat,secure=True).content.decode()
    print("Chave protegida, configuração de IA, isolamento da conversa e desconexão verificados com provedor de teste.")
    print("Catálogo, edição, consulta, histórico privado, idempotência, CSRF e isolamento entre usuários verificados.")


def select_institution_agent(client, tenant, space, agent, enabled, http):
    from bs4 import BeautifulSoup
    url = reverse("manage_agents", args=[tenant,space])
    page = client.get(url, secure=True)
    assert page.status_code == 200
    form = BeautifulSoup(page.content, "html.parser").select_one('button[value="select_agents"]').find_parent("form")
    previous = {item["value"] for item in form.select('input[name="agents"][checked]')}
    selected = previous | {str(agent)} if enabled else previous - {str(agent)}
    return client.post(url, {"csrfmiddlewaretoken":form.select_one('[name="csrfmiddlewaretoken"]')["value"],
                             "action":"select_agents", "agents":sorted(selected), "previous":sorted(previous)}, **http)


def verify_institution_agents(owner, viewer, tenant, first, second, http):
    from bs4 import BeautifulSoup
    listing = reverse("institution_agents", args=[tenant])
    csrf = owner.cookies["__Host-hubia-csrf"].value
    form = {"kind":"knowledge","knowledge_text":"Base QA","name":"Base institucional QA","description":"Orienta os membros.", "active":"on",
            "csrfmiddlewaretoken":csrf}
    assert viewer.get(listing, secure=True).status_code == 404
    assert owner.post(listing, form, **http).status_code == 302
    def by_name(name):
        sql = "SELECT id FROM hub_tenancy.agents WHERE tenant_id=%s AND workspace_id IS NULL AND name=%s"
        with connection.cursor() as cursor:
            cursor.execute(sql,[tenant,name])
            return cursor.fetchone()[0]
    agent = by_name(form["name"])
    edit = reverse("institution_agent_edit",args=[tenant,agent])
    use = reverse("use_agent",args=[tenant,first,agent])
    assert viewer.get(use,secure=True).status_code == 404
    assert select_institution_agent(owner,tenant,first,agent,True,http).status_code == 302
    assert viewer.get(use,secure=True).status_code == 200
    assert viewer.get(reverse("use_agent",args=[tenant,second,agent]),secure=True).status_code == 404
    assert select_institution_agent(owner,tenant,second,agent,True,http).status_code == 302
    assert owner.post(edit,{**form, "description":"Descrição compartilhada"},**http).status_code == 302
    assert "Descrição compartilhada" in viewer.get(use,secure=True).content.decode()
    def panel_counts():
        public = BeautifulSoup(viewer.get("/conta/espacos/",secure=True).content,"html.parser")
        return {a["href"]:int(a.select_one(".space-count").get_text().split()[0]) for a in public.select("a.space-row")}
    counts = panel_counts()
    key = reverse("workspace",args=[tenant,first])
    assert select_institution_agent(owner,tenant,first,agent,False,http).status_code == 302
    assert viewer.get(use,secure=True).status_code == 404 and panel_counts()[key] == counts[key]-1
    assert select_institution_agent(owner,tenant,first,agent,True,http).status_code == 302
    assert owner.post(listing,{**form,"name":"Excluir QA"},**http).status_code == 302
    temporary = by_name("Excluir QA")
    assert select_institution_agent(owner,tenant,first,temporary,True,http).status_code == 302
    delete = reverse("institution_agent_edit",args=[tenant,temporary]) + "?excluir=1"
    assert owner.get(delete,secure=True).status_code == 200
    assert owner.post(delete,{"action":"delete","csrfmiddlewaretoken":csrf},**http).status_code == 302
    assert viewer.get(reverse("use_agent",args=[tenant,first,temporary]),secure=True).status_code == 404
    assert panel_counts()[key] == counts[key]
    return agent
