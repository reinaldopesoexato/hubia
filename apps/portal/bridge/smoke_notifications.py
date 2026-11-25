from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.urls import reverse

from .models import Link, NotificationDestination
from .mcp import call_tool
from .access import context
from notification_channels import CHANNELS
from portal_identity import bind_account


def verify_notifications(client, outsider, user, http):
    with connection.cursor() as cursor:
        bind_account(cursor,user)
        cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships WHERE role='personal_owner'")
        tenant = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)",[str(tenant)])
        cursor.execute("SELECT id FROM hub_tenancy.workspaces WHERE tenant_id=%s",[tenant])
        space = cursor.fetchone()[0]
        cursor.execute("""INSERT INTO hub_tenancy.agents(tenant_id,workspace_id,kind,name)
                          VALUES (%s,%s,'notifications','Notificações teste') RETURNING id""",[tenant,space])
        agent = cursor.fetchone()[0]
    config = reverse("notification_config",args=[tenant,space,agent])
    page = reverse("use_agent",args=[tenant,space,agent])
    csrf = {"csrfmiddlewaretoken":client.cookies["__Host-hubia-csrf"].value}
    assert outsider.get(config,secure=True).status_code==404
    assert "Nenhum canal de envio habilitado" in client.get(page,secure=True).content.decode()
    settings = {**csrf,"action":"settings","integration":"qa","key":"chave-ficticia","enabled":"on"}
    assert client.post(config,settings,**http).status_code==302
    assert 'name="destinatario"' in client.get(page,secure=True).content.decode()
    assert client.post(config,{**csrf,"action":"destination","name":"QA","address":"123-group","sector":space},**http).status_code==302
    assert "chave-ficticia" not in client.get(config,secure=True).content.decode()
    dest = NotificationDestination.objects.get(channel__agent=agent)
    payload = {**csrf,"request_id":str(uuid4()),"mensagem":"Teste isolado","canal":"whatsapp","destino":str(dest.pk)}
    assert client.post(page,{**payload,"canal":"sms"},**http).status_code==400
    assert outsider.post(page,payload,**http).status_code==404
    sender = user.get_full_name() or user.username
    portal_message = f"_Enviado por {sender} através de Notificações teste_\n\nTeste isolado"
    calls = []
    def fake_send(conf, address, msg):
        calls.append((address,msg))
        return "done","Resposta HTTP simulada."
    with patch.dict(CHANNELS, {"whatsapp":("WhatsApp",fake_send)}):
        with TestCase.captureOnCommitCallbacks(execute=True):
            assert client.post(page,payload,**http).status_code==302
            assert client.post(page,payload,**http).status_code==302
        assert calls==[("123-group",portal_message)]
        direct = {**payload, "request_id":str(uuid4()), "destinatario":"+5511999990000"}
        del direct["destino"]
        with TestCase.captureOnCommitCallbacks(execute=True):
            assert client.post(page,direct,**http).status_code==302
            assert client.post(page,direct,**http).status_code==302
            assert client.post(page,{**direct,"destinatario":"123-group"},**http).status_code==400
            assert client.post(page,{**direct,"destino":str(dest.pk)},**http).status_code==400
        assert calls[-1]==("5511999990000",portal_message) and len(calls)==2
        NotificationDestination.objects.create(channel=dest.channel,name="Privado",sector=uuid4(),address="456-group")
        assert client.post(page,{**direct,"destinatario":"456@g.us","request_id":str(uuid4())},**http).status_code==400
        link = Link(user=user,tenant=tenant,space=space,name="Dot · ChatGPT")
        ctx = context(link)
        info = call_tool(link,ctx,False,{"name":"descrever_envio"})
        schema = info["esquema"]
        assert schema["type"]=="object" and "oneOf" not in schema
        assert set(schema["required"])=={"agent_id","canal","mensagem","request_id"}
        assert set(schema["properties"])==set(schema["required"]) | {"destinatario","destino","space_id"}
        for example in info["exemplos_para_preencher"]:
            assert set(example)>=set(schema["required"]) and len(set(example)&{"destino","destinatario"})==1
        for invalid in ({}, {"destinatario":"a","destino":"1","agent_id":str(agent),"mensagem":"X",
                            "request_id":str(uuid4()),"canal":"whatsapp"}):
            try:
                call_tool(link,{**ctx,"can_notify":True},False,{"name":"enviar_aviso","arguments":invalid})
            except ValueError as exc:
                assert all(field in str(exc) for field in schema["properties"])
            else:
                raise AssertionError("Envio aceito com campos inválidos.")
        args = {"agent_id":str(agent),"canal":"whatsapp","mensagem":"Teste MCP","destinatario":"789-group","request_id":str(uuid4())}
        with TestCase.captureOnCommitCallbacks(execute=True):
            result = call_tool(link,{**ctx,"can_notify":True},False,{"name":"enviar_aviso","arguments":args})
            assert result["destino"]=="789-group"
        assert calls[-1]==("789-group",f"_Enviado por {sender} através de Dot · ChatGPT_\n\nTeste MCP") and len(calls)==3
        try:
            call_tool(link,ctx,True,{"name":"enviar_aviso","arguments":args})
        except ValueError:
            pass
        else:
            raise AssertionError("Envio MCP sem permissão específica.")
    assert "Resposta HTTP simulada" in client.get(page,secure=True).content.decode()
    assert client.post(page,{**payload,"mensagem":"Outra mensagem"},**http).status_code==400
    assert client.post(config,{**settings,"enabled":"","key":""},**http).status_code==302
    assert client.post(page,{**payload,"request_id":str(uuid4())},**http).status_code==400
    print("Notificações: assinatura do usuário e da origem no WhatsApp, destinatário direto, atalhos, acesso e idempotência verificados com envio simulado.")
