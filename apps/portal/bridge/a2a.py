import hashlib
import json
from uuid import uuid4
from urllib.parse import urlsplit

from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from agent_runtime import search_knowledge, terms
from space_views import catalog
from .access import ICON_URL, ORIGIN, context
from .mcp import fail, success
from .models import Link, Ticket
from .network import RemoteError, public_json
from .oauth import bearer

RPC = ORIGIN + "/conta/a2a"
CARD = ORIGIN + "/.well-known/agent-card.json"


@require_GET
def card(request):
    return JsonResponse({"name":"HUB IA","iconUrl":ICON_URL,"description":"Catálogo e consulta textual às bases ativas do espaço autorizado.",
        "version":"1.0.0","supportedInterfaces":[{"protocolBinding":"JSONRPC","protocolVersion":"1.0","url":RPC}],
        "capabilities":{},"defaultInputModes":["text/plain"],"defaultOutputModes":["text/plain"],
        "securitySchemes":{"hubBearer":{"httpAuthSecurityScheme":{"scheme":"Bearer"}}},
        "securityRequirements":[{"schemes":{"hubBearer":{"list":[]}}}],
        "skills":[{"id":"space_lookup","name":"Consultar espaço","description":"Envie «listar agentes» para listar o catálogo ou envie termos da base de conhecimento.",
                   "tags":["catalog","knowledge"],"examples":["listar agentes"]}]})


@csrf_exempt
@never_cache
def endpoint(request):
    if request.method!="POST":
        return HttpResponse(status=405,headers={"Allow":"POST"})
    if request.headers.get("Origin") not in (None,ORIGIN):
        return HttpResponse(status=403)
    if request.content_type not in ("application/json","application/a2a+json") or len(request.body)>32768:
        return HttpResponse(status=400)
    key = bearer(request)
    with transaction.atomic():
        link = Link.objects.select_related("user").filter(protocol="a2a",key_digest=hashlib.sha256(key.encode()).hexdigest()).first() if key else None
        if not link:
            return HttpResponse(status=401,headers={"WWW-Authenticate":'Bearer realm="HUB A2A"'})
        try:
            ctx = context(link)
        except Http404:
            return HttpResponse(status=403)
        try:
            msg = json.loads(request.body)
        except ValueError:
            return fail(None,-32700,"JSON inválido")
        if not isinstance(msg,dict) or msg.get("jsonrpc")!="2.0" or "id" not in msg:
            return fail(None,-32600,"Requisição inválida")
        ident = msg["id"]
        if request.headers.get("A2A-Version","1.0")!="1.0":
            return fail(ident,-32009,"Versão incompatível")
        if msg.get("method") in ("GetTask","CancelTask"):
            return fail(ident,-32001,"Tarefa inexistente")
        if msg.get("method")!="SendMessage":
            return fail(ident,-32601,"Método indisponível")
        params = msg.get("params")
        message = params.get("message") if isinstance(params,dict) else None
        if not isinstance(message,dict) or message.get("role")!="ROLE_USER" or not isinstance(message.get("messageId"),str) or not message["messageId"]:
            return fail(ident,-32602,"Mensagem inválida")
        if message.get("taskId"):
            return fail(ident,-32001,"Tarefa inexistente")
        parts = message.get("parts")
        if not isinstance(parts,list) or not parts or any(not isinstance(p,dict) or not isinstance(p.get("text"),str) for p in parts):
            return fail(ident,-32005,"Somente texto é suportado")
        question = " ".join(p["text"] for p in parts).strip()
        if not 1<=len(question)<=2000:
            return fail(ident,-32602,"Informe até 2.000 caracteres")
        with connection.cursor() as cursor:
            if question.casefold() in ("listar agentes","list agents","catalog"):
                answer = "\n".join(a["name"] for a in catalog(cursor,link.tenant,link.space)) or "Catálogo vazio."
            elif question.casefold() in ("listar avisos", "list notices"):
                notices = Ticket.objects.filter(link=link, notice_agent__isnull=False, state="pending").order_by("created")[:25]
                batch = list(notices.select_for_update())
                answer = "\n\n".join(t.question for t in batch) or "Nenhum aviso pendente."
                Ticket.objects.filter(pk__in=[t.pk for t in batch]).update(state="read")
            elif ctx["viewer"]:
                return HttpResponse(status=403)
            else:
                cursor.execute("""SELECT name,configuration FROM hub_tenancy.agents WHERE tenant_id=%s
                    AND workspace_id=%s AND active AND kind='knowledge' ORDER BY name""",[link.tenant,link.space])
                hits = [name+"\n"+search_knowledge(body,question) for name,body in cursor.fetchall() if terms(question)&terms(body)]
                answer = "\n\n".join(hits)[:16000] or "Nenhum trecho correspondente nas bases deste espaço."
        reply = {"role":"ROLE_AGENT","messageId":str(uuid4()),"parts":[{"text":answer}]}
        if isinstance(message.get("contextId"),str):
            reply["contextId"] = message["contextId"]
        return success(ident,{"message":reply})


def query_peer(config, question, request_id, task_id=None):
    """A URL do card é escolhida pelo operador. O Bearer fica restrito à mesma origem."""
    card_url = config["card_url"]
    descriptor = public_json(card_url)
    source = urlsplit(card_url)
    interfaces = descriptor.get("supportedInterfaces")
    if not isinstance(interfaces,list):
        raise RemoteError("Agent Card sem interfaces A2A 1.0.")
    for iface in interfaces:
        if not isinstance(iface,dict) or (iface.get("protocolBinding"),iface.get("protocolVersion"))!=("JSONRPC","1.0"):
            continue
        target = iface.get("url")
        if isinstance(target,str):
            dest = urlsplit(target)
            if (dest.scheme,dest.hostname,dest.port or 443)==("https",source.hostname,source.port or 443):
                break
    else:
        raise RemoteError("Nenhuma interface JSON-RPC 1.0 da mesma origem do card.")
    headers = {"Content-Type":"application/json","A2A-Version":"1.0"}
    if config.get("token"):
        headers["Authorization"]="Bearer "+config["token"]
    params = {"id":task_id} if task_id else {"message":{"role":"ROLE_USER","messageId":str(request_id),
        "parts":[{"text":question}]},"configuration":{"returnImmediately":True}}
    method = "GetTask" if task_id else "SendMessage"
    msg = {"jsonrpc":"2.0","id":str(request_id),"method":method,"params":params}
    res = public_json(target,json.dumps(msg).encode(),headers)
    result = res.get("result")
    if res.get("id")!=msg["id"] or res.get("jsonrpc")!="2.0" or not isinstance(result,dict):
        raise RemoteError("Resposta JSON-RPC inválida ou solicitação recusada.")
    if "message" in result:
        return "answered",text_parts(result["message"]),""
    task = result if task_id else result.get("task",{})
    if not isinstance(task,dict) or not isinstance(task.get("id"),str) or not 1<=len(task["id"])<=256:
        raise RemoteError("Tarefa A2A inválida.")
    state = task.get("status",{}).get("state")
    if state=="TASK_STATE_COMPLETED":
        answer = "\n".join(text_parts(a) for a in task.get("artifacts",[]))
        return "answered",answer or text_parts(task["status"].get("message",{})),task["id"]
    if state in ("TASK_STATE_SUBMITTED","TASK_STATE_WORKING"):
        return "waiting","",task["id"]
    if state in ("TASK_STATE_AUTH_REQUIRED","TASK_STATE_INPUT_REQUIRED"):
        return "attention",text_parts(task["status"].get("message",{})),task["id"]
    return "failed",str(state or "Estado desconhecido"),task["id"]


def text_parts(message):
    if not isinstance(message,dict):
        raise RemoteError("Parte A2A inválida.")
    return "\n".join(p["text"] for p in message.get("parts",[]) if isinstance(p,dict) and isinstance(p.get("text"),str))[:16000]
