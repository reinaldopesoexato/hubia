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
    assert location in client.get("/conta/",secure=True).content.decode()
    assert client.get(location,secure=True).status_code==200
    assert ordinary.get(location,secure=True).status_code==404
    assert ordinary.get(manage,secure=True).status_code==404
    csrf = client.cookies["__Host-hubia-csrf"].value
    form = {"kind":"knowledge","name":"Procedimentos","configuration":"Atendimento das 8 às 18 horas.","active":"on","csrfmiddlewaretoken":csrf}
    assert client.post(manage,form,**http).status_code==302
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
