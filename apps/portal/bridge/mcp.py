import json
from uuid import UUID
from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache

from space_views import catalog
from .access import MCP_ICONS, ORIGIN, email_title, with_icon_link
from .events import EVENT_INFO, EMPTY_SCHEMA, subscribe
from . import discovery, library, mailbox_service, notifications
from .models import LibraryAccess
from .models import Ticket
from .network import RemoteError
from .oauth import authenticate_mcp, unauthorized

VERSIONS = ("2025-11-25","2025-06-18","2025-03-26")


def fail(ident,code,message,data=None):
    err = {"code":code,"message":message}
    if data is not None:
        err["data"] = data
    return JsonResponse({"jsonrpc":"2.0","id":ident,"error":err})


def success(ident,result):
    return JsonResponse({"jsonrpc":"2.0","id":ident,"result":result})


def schema(fields):
    return {"type":"object","properties":{k:{"type":"string"} for k in fields},
            "required":list(fields),"additionalProperties":False}


def tool(name,description,fields=(),write=False):
    return {"name":name,"description":description,"inputSchema":schema(fields),"icons":MCP_ICONS,
            "annotations":{"readOnlyHint":not write,"destructiveHint":False,"openWorldHint":False,
                           "idempotentHint":True}}


TOOLS = [tool("list_agents","Listar agentes ativos dos espaços autorizados, com o nome e o ID de cada espaço."),
         tool("list_tasks","Ver as 50 tarefas pendentes mais antigas desta conexão."),
         tool("fetch_task","Ler uma tarefa privada desta conexão.",["task_id"])]
REPLY = tool("reply_task","Entregar uma resposta definitiva a uma tarefa pendente desta conexão.",
             ["task_id","answer"],write=True)


LIST_DESTINATIONS = tool("listar_destinos", "Listar atalhos de destinos salvos. Um destinatário direto dispensa esta consulta.", ["agent_id"])
DESCRIBE_NOTIFY = tool("descrever_envio", "Consultar os campos obrigatórios, formatos e exemplos JSON de enviar_aviso, sem enviar mensagem. Não exige argumentos.")
NOTIFY = tool(
    "enviar_aviso",
    "Enviar um aviso externo. Obrigatórios: agent_id (UUID obtido com list_agents), canal (whatsapp), "
    "mensagem (1 a 2.000 caracteres) e request_id (UUID novo por aviso, reutilizado nas tentativas do mesmo aviso). "
    "Informe exatamente um: destinatario (telefone com DDI ou ID exato do grupo) ou destino "
    "(ID textual de atalho obtido com listar_destinos). Para exemplos sem envio, chame descrever_envio.",
    write=True,
)
NOTIFY["annotations"]["openWorldHint"] = True
NOTIFY["inputSchema"] = {
    "type": "object",
    "properties": {
        "agent_id": {"type":"string", "format":"uuid", "description":"UUID do agente de notificações, obtido com list_agents."},
        "canal": {"type":"string", "enum":list(notifications.CHANNELS), "description":"Canal habilitado no agente."},
        "mensagem": {"type":"string", "minLength":1, "maxLength":2000, "description":"Texto do aviso. No WhatsApp, o HUB acrescenta a identificação do usuário e da conexão."},
        "request_id": {"type":"string", "format":"uuid", "description":"Identificador único do aviso. Reutilize o UUID e os mesmos campos ao repetir a solicitação."},
        "destinatario": {"type":"string", "description":"Endereço direto. Para WhatsApp, use telefone com DDI ou ID de grupo no formato dígitos-group ou dígitos@g.us."},
        "destino": {"type":"string", "description":"ID de um destino salvo conforme listar_destinos. Use como alternativa a destinatario."},
    },
    "required": ["agent_id","canal","mensagem","request_id"],
    "additionalProperties": False,
}


def describe_notification():
    example = {"agent_id":"<UUID retornado por list_agents>", "canal":"whatsapp",
               "mensagem":"Aviso do AviHub", "request_id":"<UUID novo para este aviso>"}
    return {
        "ferramenta": NOTIFY["name"], "descricao": NOTIFY["description"],
        "esquema": NOTIFY["inputSchema"],
        "exemplos_para_preencher": [
            {**example, "destinatario":"<ID exato do grupo já utilizado>"},
            {**example, "destino":"<ID retornado por listar_destinos>"},
        ],
    }


SEARCH_NOTES = tool("buscar_notas", "Pesquisar títulos, caminhos e texto Markdown nas pastas liberadas ao espaço. Se houver proxima_pagina, continue com a mesma consulta e revisao.", ["consulta"])
READ_NOTE = tool("ler_nota", "Ler uma nota autorizada e obter o link da fonte. Use o caminho retornado por buscar_notas. Para continuar, use proximo_inicio e revisao.", ["caminho"])
for spec, page_field in ((SEARCH_NOTES, "pagina"), (READ_NOTE, "inicio")):
    spec["annotations"]["openWorldHint"] = True
    spec["inputSchema"]["properties"].update({
        page_field:{"type":"string", "pattern":"^[0-9]{1,8}$"}, "revisao":{"type":"string"}
    })


LIST_MAIL = tool("listar_emails_novos", "Lista até dez mensagens da sua INBOX. Na primeira consulta retorna as mais recentes. Guarde o marcador para buscar novidades. Se ha_mais for verdadeiro, continue com o marcador retornado.")
LIST_MAIL["inputSchema"]["properties"]["marcador"] = {"type":"string", "description":"Marcador devolvido na consulta anterior desta caixa."}
READ_MAIL = tool("ler_email", "Lê o texto de uma mensagem da sua INBOX sem marcá-la como lida. Use o mensagem_id retornado por listar_emails_novos.", ["mensagem_id"])
for spec in (LIST_MAIL, READ_MAIL):
    spec["annotations"]["openWorldHint"] = True


SEND_MAIL = tool("enviar_email",
    "Envia um e-mail pela sua caixa institucional. Reutilize o mesmo request_id e os mesmos campos "
    "para consultar uma tentativa. aceito_pelo_smtp indica aceite do provedor. Em estado incerto "
    "ou registrado, confira o recebimento antes de iniciar outro envio.",
    ["destinatario", "assunto", "mensagem", "request_id"], write=True)
SEND_MAIL["annotations"].update(openWorldHint=True, destructiveHint=True)
SEND_MAIL["inputSchema"]["properties"] = {
    "destinatario":{"type":"string", "format":"email", "maxLength":254, "description":"Um único endereço de e-mail."},
    "assunto":{"type":"string", "minLength":1, "maxLength":200, "description":"Assunto, em uma linha."},
    "mensagem":{"type":"string", "minLength":1, "maxLength":8000, "description":"Corpo em texto simples. O HUB acrescenta a assinatura salva automaticamente."},
    "request_id":{"type":"string", "format":"uuid", "description":"Um UUID novo por mensagem, mantido nas repetições."},
}


READ_SIGNATURE = tool("consultar_assinatura_email",
    "Consultar a assinatura da sua caixa usada apenas nos e-mails enviados por IAs pelo HUB.")
EDIT_SIGNATURE = tool("editar_assinatura_email",
    "Alterar a assinatura compartilhada pelos envios de IA da sua caixa nesta instituição. "
    "Use quando o usuário pedir para alterar ou remover a assinatura. Não envia e-mail.",
    ["html"], write=True)
EDIT_SIGNATURE["annotations"]["destructiveHint"] = True
EDIT_SIGNATURE["inputSchema"]["properties"]["html"].update(maxLength=6000,
    description="HTML simples com parágrafos, quebras, negrito, itálico, sublinhado, listas e links HTTPS, "
                "mailto ou tel. Use uma string vazia para remover.")


LIST_SPACES = tool("listar_espacos", "Listar os espaços autorizados desta conexão e obter seu space_id.")
SPACE_TOOLS = {t["name"] for t in (SEARCH_NOTES, READ_NOTE, LIST_DESTINATIONS, NOTIFY)}
for spec in (SEARCH_NOTES, READ_NOTE, LIST_DESTINATIONS, NOTIFY):
    spec["inputSchema"]["properties"]["space_id"] = {"type":"string", "format":"uuid",
        "description":"ID obtido em listar_espacos. Obrigatório quando a conexão acessa vários espaços."}
NOTIFY["description"] += " Indique também space_id quando houver vários espaços autorizados."


def available_tools(link,ctx,can_reply):
    spaces = ctx.get("spaces", {link.space: ""})
    if getattr(link, "email_only", False):
        return mail_tools(link, ctx)
    result = [LIST_SPACES, TOOLS[0]]
    if link.space in spaces:
        result.extend(TOOLS[1:])
        if can_reply:
            result.append(REPLY)
    with connection.cursor() as cursor:
        cursor.execute("""SELECT EXISTS(SELECT FROM hub_tenancy.agents WHERE tenant_id=%s
                           AND workspace_id=ANY(%s::uuid[]) AND kind='notifications' AND active)""",
                       [link.tenant, list(spaces)])
        if cursor.fetchone()[0]:
            result.extend([LIST_DESTINATIONS,DESCRIBE_NOTIFY])
            if ctx.get("can_notify"):
                result.append(NOTIFY)
    if LibraryAccess.objects.filter(library__connection_id=link.tenant, space__in=spaces).exists():
        result.extend([SEARCH_NOTES, READ_NOTE])
    result.extend(mail_tools(link, ctx))
    return result


def mail_tools(link, ctx):
    result = []
    if getattr(link, 'pk', None) and mailbox_service.authorized(link).exists():
        result.extend([LIST_MAIL, READ_MAIL])
    if ctx.get("can_send_mail") and mailbox_service.sendable(link).exists():
        result.extend([SEND_MAIL, READ_SIGNATURE, EDIT_SIGNATURE])
    return result


def agent_entries(link, spaces):
    with connection.cursor() as cursor:
        return [{"id":str(a["id"]),"name":a["name"],"description":a["description"],
                 "space_id":str(space), "space_name":label}
                for space,label in spaces.items() for a in catalog(cursor,link.tenant,space)]


def ticket_data(t):
    data = {"id":str(t.pk),"request":t.question,"state":t.state,"answer":t.answer,"created":t.created.isoformat()}
    if t.notice_agent:
        data.update(type="hub.agent.available", agent_id=str(t.notice_agent))
    return data


def call_tool(link,ctx,can_reply,params):
    args = params.get("arguments",{})
    name = params.get("name")
    available = available_tools(link,ctx,can_reply)
    spec = next((t for t in available if t["name"]==name),None)
    if not spec:
        raise ValueError("Ferramenta indisponível.")
    if name=="enviar_aviso" and not isinstance(args,dict):
        raise ValueError(NOTIFY["description"])
    if not isinstance(args,dict):
        raise ValueError("Ferramenta indisponível ou argumentos inválidos.")
    inputs = spec["inputSchema"]
    if (not set(inputs["required"]) <= args.keys() <= inputs["properties"].keys()
            or any(not isinstance(value,str) for value in args.values())
            or (name=="enviar_aviso" and len({"destinatario","destino"} & args.keys()) != 1)):
        if name=="enviar_aviso":
            raise ValueError(NOTIFY["description"])
        raise ValueError("Ferramenta indisponível ou argumentos inválidos.")
    spaces = ctx.get("spaces", {link.space: ctx.get("workspace", "")})
    if name == "listar_espacos":
        return [{"space_id":str(key), "nome":label} for key,label in spaces.items()]
    if name in SPACE_TOOLS:
        if "space_id" in args:
            space = UUID(args["space_id"])
            if space not in spaces:
                raise ValueError("Espaço indisponível para esta conexão.")
        elif len(spaces) == 1:
            space = next(iter(spaces))
        else:
            raise ValueError("Informe o space_id do espaço desejado. Consulte listar_espacos.")
    if name in ("consultar_assinatura_email", "editar_assinatura_email"):
        return mailbox_service.signature(link, args)
    if name == "enviar_email":
        return mailbox_service.send(link, args)
    if name in ("listar_emails_novos", "ler_email"):
        return mailbox_service.query(link, name, args)
    if name in ("buscar_notas", "ler_nota"):
        return library.query(link.tenant, space, name, args)
    if name=="descrever_envio":
        return describe_notification()
    if name in ("listar_destinos","enviar_aviso"):
        agent = UUID(str(args["agent_id"]))
        with connection.cursor() as cursor:
            if name=="enviar_aviso":
                return notifications.send(cursor,link.user,{**ctx,"space":space},agent,args, connection_name=link.name)
            if not any(a["id"]==agent and a["kind"]=="notifications" for a in catalog(cursor,link.tenant,space)):
                raise ValueError("Agente indisponível.")
            return [{"destino":str(d.pk),"nome":d.name,"canal":d.channel.channel}
                    for d in notifications.destinations(cursor,link.tenant,space,agent,ctx.get("spaces"))]
    if name=="list_agents":
        return agent_entries(link, spaces)
    if name=="list_tasks":
        return [ticket_data(t) for t in Ticket.objects.filter(link=link,state="pending").order_by("created")[:50]]
    task = Ticket.objects.select_for_update().filter(link=link,pk=UUID(str(args["task_id"]))).first()
    if not task:
        raise ValueError("Tarefa indisponível.")
    if task.notice_agent and task.state == "stopped":
        raise ValueError("Aviso indisponível.")
    if name == "fetch_task" and task.notice_agent and task.state == "pending":
        task.state = "read"
        task.save(update_fields=["state"])
    if name=="reply_task":
        answer = args["answer"]
        if not isinstance(answer,str) or not 1<=len(answer.strip())<=8000:
            raise ValueError("Resposta inválida.")
        if task.state=="pending" or (task.notice_agent and task.state=="read"):
            task.state,task.answer = "answered",answer.strip()
            task.save(update_fields=["state","answer"])
        elif task.state!="answered" or task.answer!=answer.strip():
            raise ValueError("Tarefa encerrada.")
    return ticket_data(task)


@csrf_exempt
@never_cache
@with_icon_link
def endpoint(request, email_only=False):
    if request.headers.get("Origin") not in (None,ORIGIN):
        return HttpResponse(status=403)
    if request.headers.get("MCP-Protocol-Version",VERSIONS[0]) not in VERSIONS:
        return HttpResponse(status=400)
    if request.method == "GET":
        return discovery.listen(request, email_only)
    if request.method!="POST":
        return HttpResponse(status=405,headers={"Allow":"GET, POST"})
    if request.content_type!="application/json" or len(request.body)>32768:
        return HttpResponse(status=415 if request.content_type!="application/json" else 413)
    try:
        msg = json.loads(request.body)
    except (ValueError,UnicodeError):
        return fail(None,-32700,"JSON inválido")
    if not isinstance(msg,dict) or msg.get("jsonrpc")!="2.0" or not isinstance(msg.get("method"),str):
        return fail(None,-32600,"Requisição inválida")
    ident,method,params = msg.get("id"),msg["method"],msg.get("params",{})
    if not isinstance(params,dict):
        return fail(ident,-32602,"Parâmetros inválidos")
    announce = False
    try:
        with transaction.atomic():
            auth = authenticate_mcp(request, email_only)
            if not auth:
                return unauthorized(email_only)
            link,ctx,can_reply = auth
            if "id" not in msg:
                return HttpResponse(status=202)
            if method=="initialize":
                ver = params.get("protocolVersion")
                result = {"protocolVersion":ver if ver in VERSIONS else VERSIONS[0],
                          "capabilities":{"tools":{"listChanged":True},"events":{}},"serverInfo":{"name":"hub-ia","version":"1.0.0","icons":MCP_ICONS}}
                if email_only:
                    title = email_title(ctx["organization"])
                    result["serverInfo"].update(name=title, title=title)
                    del result["capabilities"]["events"]
                if result["protocolVersion"] == "2025-11-25":
                    result["serverInfo"].update(title=result["serverInfo"].get("title", "HUB IA"), websiteUrl=ORIGIN + "/")
            elif method=="ping":
                result = {}
            elif method=="tools/list":
                result = discovery.list_tools(auth)
            elif method=="tools/call":
                payload = call_tool(link,ctx,can_reply,params)
            elif method=="events/list" and not email_only:
                result = {"events":[EVENT_INFO] if link.space in ctx["spaces"] else []}
            elif method in ("events/subscribe","events/unsubscribe") and not email_only:
                if method=="events/subscribe" and link.space not in ctx["spaces"]:
                    raise ValueError("Assinaturas de tarefas exigem acesso ao espaço de origem.")
                result = subscribe(link,params,remove=method=="events/unsubscribe")
            else:
                return fail(ident,-32601,"Método indisponível")
            if method in ("ping", "tools/call") and discovery.accepts_stream(request):
                announce = discovery.claim_change(auth)
    except Http404:
        return unauthorized(email_only)
    except ValueError as exc:
        return fail(ident,-32602,str(exc))
    except RemoteError:
        return fail(ident,-32015,"CallbackEndpointError",{"reason":"challenge_failed"})
    if method == "tools/call":
        result = {"content":[{"type":"text", "text":json.dumps(payload, ensure_ascii=False)}]}
    if announce:
        return discovery.post_stream(ident, result)
    return success(ident,result)
