import base64
import hashlib
import hmac
import json
import secrets
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import Client
from django.utils import timezone
from .access import MCP
from .events import EVENT
from .models import Link, Ticket, Delivery
from .portal import create_connection
from .worker import deliver_one
from portal_identity import bind_account


class Hidden(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self,tag,attrs):
        data = dict(attrs)
        if tag=="input" and data.get("type")=="hidden" and data.get("name"):
            self.fields[data["name"]] = data.get("value","")


def verify_bridge(user,other):
    client=Client(HTTP_HOST="hubia.psxt.io")
    client.force_login(user)
    anonymous=Client(HTTP_HOST="hubia.psxt.io")
    def post(url,payload,**headers):
        return anonymous.post(url,payload,content_type="application/json",secure=True,**headers)
    fresh={"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-11-25"}}
    assert post("/conta/mcp",fresh).status_code==401
    callback="https://chatgpt.com/connector/oauth/test-setup"
    reg=post("/conta/oauth/register/",{"redirect_uris":[callback],"token_endpoint_auth_method":"client_secret_basic"})
    assert reg.status_code==201,reg.content
    app=reg.json()
    verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    auth={"client_id":app["client_id"],"redirect_uri":callback,"response_type":"code","state":"test",
          "code_challenge":challenge,"code_challenge_method":"S256","scope":"hub.read hub.reply offline_access","resource":MCP}
    response=client.get("/conta/oauth/authorize/",auth,secure=True)
    assert response.status_code==200,response.status_code
    parser=Hidden()
    parser.feed(response.content.decode())
    consent=client.post("/conta/oauth/authorize/",{**parser.fields,"allow":"Autorizar"},secure=True)
    assert consent.status_code==302,(consent.status_code,consent.content[:400])
    code=parse_qs(urlsplit(consent["Location"]).query)["code"][0]
    basic=base64.b64encode((app["client_id"]+":"+app["client_secret"]).encode()).decode()
    token=anonymous.post("/conta/oauth/token/",{"grant_type":"authorization_code","code":code,"redirect_uri":callback,
                         "code_verifier":verifier,"resource":MCP},secure=True,HTTP_AUTHORIZATION="Basic "+basic)
    assert token.status_code==200,token.content
    key={"HTTP_AUTHORIZATION":"Bearer "+token.json()["access_token"]}
    def rpc(method,params):
        ans=post("/conta/mcp",{"jsonrpc":"2.0","id":11,"method":method,"params":params},**key)
        assert ans.status_code==200,ans.status_code
        return ans.json()
    from .access import MCP_ICONS
    for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
        info = rpc("initialize", {"protocolVersion":version})["result"]["serverInfo"]
        assert info["icons"] == MCP_ICONS
    for path in ("/.well-known/oauth-protected-resource/conta/mcp", "/.well-known/oauth-authorization-server"):
        metadata = anonymous.get(path, secure=True)
        assert metadata.status_code == 200 and MCP_ICONS[0]["src"] in metadata["Link"]
    assert all(spec["icons"] == MCP_ICONS for spec in rpc("tools/list", {})["result"]["tools"])
    print("Ícone do HUB IA verificado no servidor MCP, nas ferramentas e na descoberta OAuth.")
    capabilities = rpc("initialize",fresh["params"])["result"]["capabilities"]
    assert capabilities["events"] == {} and capabilities["tools"]["listChanged"]
    assert anonymous.get("/conta/mcp", secure=True, HTTP_ACCEPT="text/event-stream").status_code == 401
    assert anonymous.get("/conta/mcp", secure=True, HTTP_ACCEPT="application/json", **key).status_code == 406
    assert "result" in rpc("tools/call",{"name":"list_agents","arguments":{}})
    link=Link.objects.get(client_id=app["client_id"],user=user)
    url=f"/conta/espacos/{link.tenant}/{link.space}/integracoes/"
    outsider=Client(HTTP_HOST="hubia.psxt.io")
    outsider.force_login(other)
    assert outsider.get(url,secure=True).status_code==404
    secret=secrets.token_bytes(32)
    params={"name":EVENT,"arguments":{},"delivery":{"mode":"webhook","url":"https://example.org/events",
            "secret":"whsec_"+base64.b64encode(secret).decode()}}
    def receiver(url,raw,headers,**opts):
        sig=hmac.digest(secret,(headers["webhook-id"]+"."+headers["webhook-timestamp"]+".").encode()+raw,"sha256")
        assert headers["webhook-signature"]=="v1,"+base64.b64encode(sig).decode()
        body=json.loads(raw)
        return {"challenge":body["challenge"]} if "challenge" in body else {}
    with patch("bridge.events.public_json",side_effect=receiver):
        sub=rpc("events/subscribe",params)["result"]
        assert sub["id"]==rpc("events/subscribe",params)["result"]["id"]
        req={"action":"send","link":link.pk,"request_id":uuid4(),"question":"Teste isolado"}
        for _ in range(2):
            assert client.post(url,req,secure=True).status_code==302
        job=Ticket.objects.get(link=link)
        assert Delivery.objects.filter(ticket=job).count()==1
        # Exercita apenas a fila do teste, mesmo se já houver tarefas reais no banco.
        assert deliver_one(ticket_id=job.pk)
        assert Delivery.objects.get(ticket=job).state=="sent"
    assert "result" in rpc("tools/call",{"name":"reply_task","arguments":{"task_id":str(job.pk),"answer":"OK"}})
    job.refresh_from_db()
    assert job.answer=="OK"
    verify_discovery(client, anonymous, link, key, rpc, post)
    verify_catalog_alerts(client, link, rpc, params, receiver)
    assert rpc("events/unsubscribe",params)["result"]=={}
    with connection.cursor() as cursor:
        bind_account(cursor,user)
    l2,token2=create_connection(user,link.tenant,link.space,{"protocol":"a2a","name":"QA","writable":False})
    message={"jsonrpc":"2.0","id":"test","method":"SendMessage","params":{"message":{"messageId":"qa",
             "role":"ROLE_USER","parts":[{"text":"listar agentes"}]}}}
    assert post("/conta/a2a",message,**key).status_code==401
    answer=post("/conta/a2a",message,HTTP_AUTHORIZATION="Bearer "+token2).json()
    assert "QA Catálogo" in answer["result"]["message"]["parts"][0]["text"]
    message["params"]["message"]["parts"] = [{"text":"consultar guia"}]
    guide = post("/conta/a2a", message, HTTP_AUTHORIZATION="Bearer "+token2).json()
    assert "Titular" in guide["result"]["message"]["parts"][0]["text"]
    assert client.post(url,{"link":link.pk,"action":"revoke"},secure=True).status_code==302
    assert post("/conta/mcp",fresh,**key).status_code==401
    print("OAuth PKCE, MCP, evento assinado, fila, resposta, A2A, isolamento e revogação verificados.")

def verify_discovery(client, remote, link, key, rpc, post):
    from asgiref.sync import async_to_sync
    from django.urls import reverse
    from . import discovery

    def get_stream():
        response = remote.get("/conta/mcp", secure=True, HTTP_ACCEPT="text/event-stream", **key)
        assert response.status_code == 200 and response.is_async
        async def start():
            iterator = aiter(response.streaming_content)
            assert b"retry:" in await anext(iterator)
            return await anext(iterator)
        return async_to_sync(start)()

    def listed():
        return rpc("tools/list", {})["result"]

    def agents(result):
        return next(tool for tool in result["tools"] if tool["name"] == "list_agents")["_meta"]["io.psxt.hubia/agents"]

    initial = listed()
    assert b"keep-alive" in get_stream()
    from . import mcp
    with patch.dict(mcp.LIST_SPACES, description="Descrição atualizada no deploy"):
        assert b"notifications/tools/list_changed" in get_stream()
        assert listed()["_meta"] != initial["_meta"]
    assert listed() == initial
    manage = reverse("manage_agents", args=[link.tenant, link.space])
    form = {"kind":"knowledge", "knowledge_text":"Conteúdo QA", "name":"Descoberta MCP QA", "active":"on"}
    assert client.post(manage, form, secure=True).status_code == 302
    assert b"notifications/tools/list_changed" in get_stream()
    assert b"keep-alive" in get_stream()  # Another stream does not duplicate the outstanding signal.
    updated = listed()
    assert initial["_meta"] != updated["_meta"]
    agent = next(a for a in agents(updated) if a["name"] == form["name"])
    edit = reverse("edit_agent", args=[link.tenant,link.space,agent["id"]])
    assert client.post(edit, {**form, "active":""}, secure=True).status_code == 302
    ping = {"jsonrpc":"2.0", "id":7, "method":"ping"}
    response = post("/conta/mcp", ping, HTTP_ACCEPT="application/json, text/event-stream", **key)
    assert response["Content-Type"].startswith("text/event-stream")
    frames = [json.loads(line[6:]) for line in response.content.splitlines() if line.startswith(b"data: ")]
    assert frames == [discovery.CHANGE, {"jsonrpc":"2.0","id":7,"result":{}}]
    assert listed() == initial
    assert b"keep-alive" in get_stream()
    print("Descoberta MCP: nova revisão, agente, remoção, GET SSE e atualização em POST verificados.")


def verify_catalog_alerts(client, link, rpc, params, receiver):
    from datetime import timedelta
    from django.urls import reverse
    from .models import Subscription
    from .worker import deliver_peer_notice
    from .network import RemoteError

    manage = reverse("manage_agents",args=[link.tenant, link.space])
    setting = {"action":"catalog_alerts", "enabled":"on"}
    assert client.post(manage, setting, secure=True).status_code == 302
    peer, _ = create_connection(link.user,link.tenant,link.space,{"protocol":"peer","name":"QA Peer",
        "writable":False,"card":"https://example.org/card","token":""})
    form = {"kind":"knowledge", "knowledge_text":"Conteúdo QA", "name":"QA Catálogo", "active":"on"}
    assert client.post(manage, form, secure=True).status_code == 302
    notice = Ticket.objects.get(link=link,notice_agent__isnull=False)
    edit = reverse("edit_agent",args=[link.tenant,link.space,notice.notice_agent])
    assert client.post(edit,form,secure=True).status_code == 302
    assert Ticket.objects.filter(link=link,notice_agent__isnull=False).count() == 1
    with patch("bridge.events.public_json",side_effect=receiver):
        assert deliver_one(ticket_id=notice.pk)
    assert Delivery.objects.get(ticket=notice).state == "sent"
    read = rpc("tools/call",{"name":"fetch_task","arguments":{"task_id":str(notice.pk)}})
    assert json.loads(read["result"]["content"][0]["text"])["type"] == "hub.agent.available"
    outgoing = Ticket.objects.get(link=peer,notice_agent=notice.notice_agent)
    with patch("bridge.worker.query_peer",side_effect=RemoteError("offline")) as first:
        assert deliver_peer_notice(outgoing.pk)
    Ticket.objects.filter(pk=outgoing.pk).update(notice_due=timezone.now())
    with patch("bridge.worker.query_peer",return_value=("answered","Recebido","")) as second:
        assert deliver_peer_notice(outgoing.pk)
    assert first.call_args.args[2] == second.call_args.args[2]
    assert Ticket.objects.get(pk=outgoing.pk).state == "answered"
    # O aviso fica consultável quando o cliente está sem assinatura.
    Subscription.objects.filter(link=link).update(expires=timezone.now()-timedelta(minutes=1))
    assert client.post(edit,{**form,"active":""},secure=True).status_code == 302
    assert client.post(edit,form,secure=True).status_code == 302
    pending = Ticket.objects.get(link=link,notice_agent__isnull=False,state="pending")
    assert not Delivery.objects.filter(ticket=pending).exists()
    with patch("bridge.events.public_json",side_effect=receiver):
        rpc("events/subscribe",params)
    assert Delivery.objects.filter(ticket=pending).count()==1
    assert client.post(manage,{"action":"catalog_alerts"},secure=True).status_code == 302
    pending.refresh_from_db()
    assert pending.state == "stopped"
    print("Avisos de catálogo: publicação, reativação, assinatura renovada, cancelamento e retry A2A verificados.")
