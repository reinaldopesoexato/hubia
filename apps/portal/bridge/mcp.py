import json
import re
from base64 import b64encode
from urllib.parse import urlencode
from uuid import UUID
from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache

from agent_runtime import ConnectorError, query_connector, search_knowledge
from dashboard import space_guide
from iaves_brand import DISPLAY_INSTRUCTIONS as IAVES_DISPLAY, NAME as IAVES_NAME, NAME_MARKDOWN as IAVES_NAME_MARKDOWN
from model_provider import MODEL_REF, available_connectors
from space_views import active_agent, catalog
from .access import MCP_ICONS, MCP_SERVER_INFO, ORIGIN, email_title, with_icon_link
from .events import EVENT_INFO, EMPTY_SCHEMA, subscribe
from . import avihub, avihub_api, avihub_report_api, discovery, iaves, library, mailbox_service, notifications
from .mail_protocol import SEARCH_FIELDS
from .models import ConnectionUsage, IAVES_AGENT_ID, IAvesSettings, LibraryAccess, PESO_EXATO_TENANT_ID
from .agent_catalog import visible_tasks
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


LIST_MAIL = tool("listar_emails_novos", "Lista até dez mensagens da sua INBOX. Na primeira consulta retorna as mais recentes. Guarde o marcador para buscar novidades. Se ha_mais for verdadeiro, continue com o marcador retornado. Para procurar mensagens antigas ou por remetente, use buscar_emails.")
LIST_MAIL["inputSchema"]["properties"]["marcador"] = {"type":"string", "description":"Marcador devolvido na consulta anterior desta caixa."}
READ_MAIL = tool("ler_email", "Lê o texto de uma mensagem da sua INBOX sem marcá-la como lida. Use o mensagem_id retornado por listar_emails_novos ou buscar_emails.", ["mensagem_id"])
SEARCH_MAIL = tool("buscar_emails",
    "Pesquisa o histórico da caixa de entrada por palavra ou frase. Para encontrar e-mails de uma pessoa, "
    "use campo=remetente e o nome ou endereço, por exemplo Rodrigo. Retorna até dez resultados por página, "
    "do mais recente para o mais antigo. Enquanto ha_mais for verdadeiro, repita a mesma busca com o marcador "
    "retornado, mesmo se a página estiver vazia. Use ler_email para abrir um resultado.", ["termo"])
SEARCH_MAIL["inputSchema"]["properties"] = {
    "termo":{"type":"string", "minLength":1, "maxLength":200, "description":"Palavra, nome, endereço de e-mail ou frase."},
    "campo":{"type":"string", "enum":list(SEARCH_FIELDS), "default":"texto",
             "description":"remetente para nome ou endereço, assunto para o título, texto para cabeçalhos e corpo."},
    "marcador":{"type":"string", "description":"Marcador da página anterior de buscar_emails com o mesmo termo e campo."},
}
for spec in (LIST_MAIL, READ_MAIL, SEARCH_MAIL):
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


IAVES_ROUTING = (
    "Quando consultar_iaves estiver disponível, use essa ferramenta primeiro para perguntas sobre qualquer etapa da cadeia "
    "avícola, mesmo quando outros agentes também estiverem disponíveis. Inclui genética e reprodução, incubação, manejo, "
    "nutrição, ambiência, sanidade, bem-estar, produção de carne e ovos, equipamentos, indicadores, abate, processamento, "
    "logística, comercialização e gestão. "
    "Use a base local priorizada pelo IAves para orientação e consulte o AviHUB quando precisar de registros atuais ou dados "
    "próprios da conta e da unidade autorizadas. Para operações nativas do AviHUB, use a ferramenta AviHUB correspondente."
)
PERSONAL_GUIDE = tool("consultar_guia", "Guia dos meus espaços. Consulta meu papel e os agentes ativos em cada espaço autorizado nesta conexão.")
LIST_SPACES = tool("listar_espacos", "Listar os espaços autorizados desta conexão e obter seu space_id.")
ASK_AGENT = tool("consultar_agente", "Fazer uma consulta a um agente de conhecimento ou API conectado. "
                 "Para assuntos da cadeia avícola, priorize consultar_iaves quando disponível. "
                 "Use list_agents para obter agent_id, tipo e space_id.", ("agent_id", "pergunta", "request_id"))
ASK_AGENT["inputSchema"]["properties"]["pergunta"].update(minLength=1, maxLength=2000)
ASK_AGENT["inputSchema"]["properties"]["request_id"].update(format="uuid")
ASK_AGENT["annotations"].update(openWorldHint=True, idempotentHint=False)
IAVES_QUERY = tool("consultar_iaves", f"Consultar {IAVES_NAME}, especialista da Peso Exato em toda a cadeia avícola. "
    f"{IAVES_ROUTING} {IAVES_DISPLAY} "
    f"Se usar a base local, cite somente «{iaves.SOURCE_MARKDOWN}» em Markdown ou «{iaves.SOURCE_LABEL}» em texto simples. "
    "Se o usuário pedir detalhes de uma resposta anterior, repasse a referencia_fontes dessa consulta "
    "e apresente somente os nomes dos arquivos e suas revisões. Não mostre diretórios, referências opacas ou links de repositório. "
    "Se estado for pergunta, apresente a pergunta e continue com os dados informados pelo usuário. "
    "Para dados próprios, use apenas a conta e a unidade escolhidas.",
    ("pergunta",), write=True)
IAVES_QUERY["title"] = IAVES_NAME
IAVES_QUERY["annotations"].update(openWorldHint=True,idempotentHint=False)
IAVES_QUERY["inputSchema"]["properties"].update({
    "pergunta":{"type":"string","minLength":1,"maxLength":2000,
                "description":"Pergunta do usuário sobre a cadeia avícola, com o contexto relevante da conversa."},
    "referencia_fontes":{"type":"string","minLength":1,"maxLength":iaves.SOURCE_REFERENCE_LIMIT,
                         "description":"Referência opaca devolvida na consulta anterior. Use só quando o usuário pedir detalhes das fontes."},
    "space_id":{"type":"string","format":"uuid","description":"Espaço autorizado que oferece o IAves. Opcional."},
    "avihub":{"type":"string","enum":list(iaves.AVIHUB_OPERATIONS),
              "description":"Complemente a orientação com dados atuais do AviHUB quando a dúvida depender da conta e unidade do usuário."},
    "conta":{"type":"string","description":"ID da conta AviHUB escolhida pelo usuário."},
    "unidade":{"type":"string","description":"ID ou nome da unidade AviHUB escolhida pelo usuário."},
    "serie":{"type":"string","description":"Número de série do equipamento."},
    "modelo":{"type":"string","description":"Modelo do equipamento, quando conhecido."},
    "nucleo_id":{"type":"string","description":"ID do núcleo autorizado."},
    "inicio":{"type":"string","format":"date","description":"Data inicial das pesagens, em AAAA-MM-DD."},
    "fim":{"type":"string","format":"date","description":"Data final das pesagens, em AAAA-MM-DD."},
    "horas":{"type":"string","enum":["24","168"],"description":"Janela de equipamento sem comunicação. Padrão de 24 horas."},
})
IAVES_REMEMBER = tool("memorizar_iaves", "Salvar na memória privada do IAves uma informação ou preferência que o usuário "
    "pediu para lembrar ou que ele informou e é útil em futuras conversas. Use uma chave curta e estável. "
    "Grave somente informações do usuário relevantes para o acompanhamento avícola.", ("chave","valor"), write=True)
IAVES_REMEMBER["inputSchema"]["properties"].update({
    "chave":{"type":"string","minLength":1,"maxLength":100},
    "valor":{"type":"string","minLength":1,"maxLength":1000},
    "space_id":{"type":"string","format":"uuid","description":"Espaço autorizado que oferece o IAves. Opcional."},
})
SCOPED_TOOLS = (SEARCH_NOTES, READ_NOTE, LIST_DESTINATIONS, NOTIFY, ASK_AGENT)
SPACE_TOOLS = {spec["name"] for spec in SCOPED_TOOLS}
for spec in SCOPED_TOOLS:
    spec["inputSchema"]["properties"]["space_id"] = {"type":"string", "format":"uuid",
        "description":"ID obtido em listar_espacos. Obrigatório quando a conexão acessa vários espaços."}
NOTIFY["description"] += " Indique também space_id quando houver vários espaços autorizados."


REPORT_OPTIONS = "opcoes_relatorio_peso_aves"
REPORT_GENERATE = "gerar_relatorio_peso_aves"
REPORT_STATUS = "consultar_relatorio_peso_aves"
AVIHUB_LIST_NOTICES = "listar_notificacoes_avihub"
AVIHUB_SEARCH_NOTICES = "buscar_notificacoes_avihub"
AVIHUB_READ_NOTICE = "consultar_notificacao_avihub"
AVIHUB_NOTICE_TOOLS = (AVIHUB_LIST_NOTICES, AVIHUB_SEARCH_NOTICES, AVIHUB_READ_NOTICE)
BIRD_WEIGHT_READS = ("consultar_pesagens", "resumir_pesagens")
REPORT_RESOURCE_BASE = "hubia://avihub/relatorios/"
REPORT_RESOURCE_FORMAT = re.compile(re.escape(REPORT_RESOURCE_BASE) + r"([0-9a-f-]{36})/([0-9a-f-]{36})")
MAX_MCP_REPORT_BYTES = 4 * 1024 * 1024
REPORT_FILE_SOURCES = {
    "AviHUB": "Arquivo original de Peso Aves do AviHUB.",
    "Prévia do AviHUB": "PDF impresso pelo Hub IA a partir da prévia oficial de Peso Aves do AviHUB.",
}
REPORT_ROUTING = (
    f"Se o usuário pedir um relatório de Peso Aves, inclusive 'Relatório PDF' após esta consulta, use a geração nativa do AviHUB. "
    f"Confira os filtros em {REPORT_OPTIONS}, solicite em {REPORT_GENERATE} e acompanhe o relatorio_id em {REPORT_STATUS}. "
    "Para a última pesagem ou seleção, use a data e o lote do evento correspondente. Confirme lote, galpão e tipo da "
    "balança nas opções nativas antes de enviar os filtros. Para seleção, use tipo_balanca correspondente à balança "
    "do evento. Use parametros_pdf do evento para reaproveitar também os filtros múltiplos. Para PDF, use formato=pdf. "
    "Entregue o link autenticado url_download quando o arquivo estiver pronto. Se fonte for 'Prévia do AviHUB', "
    "descreva como PDF da prévia oficial, impresso pelo HUB IA. "
    "Para abrir uma prévia no AviHUB, use url_previa. "
    "Se a geração não estiver disponível nesta conexão, ofereça somente o link do formulário autenticado para o usuário solicitar o relatório."
)


def avihub_tools():
    specs = {avihub_api.UNITS: tool(avihub_api.UNITS, "Listar as unidades disponíveis da conta AviHUB vinculada.")}
    specs[avihub_api.SELECT_UNIT] = tool(avihub_api.SELECT_UNIT,
        "Ativa a unidade no perfil AviHUB usando o seletor do painel. Use somente após o usuário escolher a unidade e saber que ela ficará ativa também no painel.",
        ("unidade",), write=True)
    hints = {
        "unidade": "ID ou nome da unidade escolhida pelo usuário. Havendo várias, pergunte antes de consultar.",
        "nucleo_id": "ID do núcleo autorizado, se necessário.",
        "serie": "Número de série completo, preservando hífens e zeros.", "modelo": "Exemplo: SAC.",
        "inicio": "Data inicial AAAA-MM-DD.", "fim": "Data final AAAA-MM-DD.",
        "pagina": "Página a partir de 1.", "horas": "Horas desde a última conexão.",
    }
    for name, (_, description, required, optional) in avihub_api.REPORTS.items():
        spec = tool(name, "AviHUB. " + description + " Use a unidade indicada pelo usuário.", required)
        if name in BIRD_WEIGHT_READS:
            spec["description"] += " " + REPORT_ROUTING
        fields = spec["inputSchema"]["properties"]
        for key in (*required, *optional, "unidade"):
            fields[key] = {"type": "string", "description": hints.get(key, key)}
        specs[name] = spec
    specs[REPORT_OPTIONS] = tool(REPORT_OPTIONS, "Consultar os formatos, filtros e canais de entrega autorizados da aba Peso Aves. "
        "Quando faltar uma escolha do usuário, apresente somente as opções retornadas. Informe o formato nesta consulta "
        "para verificar se o arquivo pode ser enviado como anexo.")
    specs[REPORT_GENERATE] = tool(REPORT_GENERATE, "Solicitar a geração de um arquivo nativo da aba Peso Aves. "
        "Use também para um pedido curto como 'Relatório PDF' após uma consulta de pesagens, com formato=pdf. "
        "Antes de gerar, use opcoes_relatorio_peso_aves com a conta e a unidade ativa, e use o formato e canal escolhidos pelo usuário. "
        "Se ficar pendente, informe o estado e o endereço para acompanhar o relatório. Consulte o mesmo relatorio_id "
        "para verificar a geração e a entrega. Reutilize request_id ao repetir o mesmo pedido.",
        ("inicio", "fim", "formato", "request_id"), write=True)
    hints.update({
        "formato": "ID exato do formato retornado em opcoes_relatorio_peso_aves.",
        "lote_id": "ID do lote listado nas opções da aba Peso Aves.",
        "galpao_id": "ID do galpão listado nas opções da aba Peso Aves.",
        "lado_id": "ID do lado listado nas opções da aba Peso Aves.",
        "box_id": "ID do box listado nas opções da aba Peso Aves.",
        "equipamento_id": "ID do equipamento listado nas opções da aba Peso Aves.",
        "tipo_balanca": "Valor confirmado nas opções do AviHUB para o tipo de balança ou modelo do evento de pesagem.",
    })
    for name in (REPORT_OPTIONS, REPORT_GENERATE):
        for field in (*avihub_report_api.FILTER_FIELDS, "unidade"):
            specs[name]["inputSchema"]["properties"][field] = {
                "type":"string", "description":hints.get(field, "ID retornado nas opções autorizadas da aba Peso Aves."),
            }
            if field in avihub_report_api.SCOPED_FILTERS and field != "nucleo_id":
                specs[name]["inputSchema"]["properties"][field]["description"] += " Para vários IDs, separe por vírgula."
    specs[REPORT_GENERATE]["inputSchema"]["properties"].update({
        "request_id":{"type":"string", "format":"uuid", "description":"UUID por pedido. Reutilize em tentativas idênticas."},
        "canal":{"type":"string", "enum":["painel", "email", "email_anexo", "whatsapp"],
                 "description":"Canal solicitado pelo usuário entre os retornados em opcoes_relatorio_peso_aves. Painel é o padrão."},
        "destino":{"type":"string", "description":"ID de um destino autorizado, retornado nas opções de WhatsApp, escolhido pelo usuário."},
    })
    specs[AVIHUB_LIST_NOTICES] = tool(AVIHUB_LIST_NOTICES,
        "Listar as notificações da Central de Notificações da conta AviHUB vinculada, da mais recente em diante.")
    specs[AVIHUB_SEARCH_NOTICES] = tool(AVIHUB_SEARCH_NOTICES,
        "Buscar avisos da conta AviHUB vinculada pela pesquisa da própria Central de Notificações.", ("busca",))
    specs[AVIHUB_READ_NOTICE] = tool(AVIHUB_READ_NOTICE,
        "Consultar uma notificação da conta AviHUB vinculada pelo ID retornado na lista ou na busca. "
        "Para abrir um resultado paginado, reutilize a página, o termo e o filtro da listagem.", ("notificacao_id",))
    for name in AVIHUB_NOTICE_TOOLS:
        fields = specs[name]["inputSchema"]["properties"]
        fields.update({
            "pagina":{"type":"string", "description":"Número da página a partir de 1."},
            "filtro":{"type":"string", "enum":["lidas", "nao_lidas", "favoritas"], "description":"Filtro opcional da central."},
        })
        if name != AVIHUB_LIST_NOTICES:
            fields["busca"] = {"type":"string", "description":"Termo a pesquisar no título, na mensagem ou no conteúdo pesquisável pelo AviHUB."}
        if name == AVIHUB_READ_NOTICE:
            fields["notificacao_id"] = {"type":"string", "description":"ID da notificação retornado pela conta consultada."}
        specs[name]["description"] += " Use o texto retornado como conteúdo da consulta e mantenha o estado de leitura no AviHUB."
    for spec in specs.values():
        spec["inputSchema"]["properties"]["conta"] = {
            "type": "string",
            "description": "ID, apelido ou e-mail da conta AviHUB indicada no contexto da conversa. "
                           "Com várias contas, consulte listar_contas_avihub e pergunte se houver ambiguidade. "
                           "Reutilize o ID nas próximas chamadas até o usuário mudar de conta."}
        spec["annotations"]["openWorldHint"] = True
    specs[avihub_api.ACCOUNTS] = tool(avihub_api.ACCOUNTS,
        "Listar as contas AviHUB vinculadas pelo usuário nesta instituição, com ID, apelido e e-mail.")
    reset = tool(avihub_api.RESET_PASSWORD,
        "Solicita ao AviHUB o envio do link de redefinição ao e-mail informado pelo usuário. "
        "Use quando o usuário pedir para recuperar a senha. Funciona antes de vincular a conta.",
        ("email",), write=True)
    reset["annotations"].update(openWorldHint=True, idempotentHint=False)
    specs[avihub_api.RESET_PASSWORD] = reset
    status = tool(REPORT_STATUS, "Consultar relatórios próprios de Peso Aves, com estado da geração, origem do arquivo e entrega. "
        "Com relatorio_id, aguarde o estado pronto, use url_download para entregar o arquivo e preserve a origem indicada em fonte. "
        "Quando o resultado indicar modo=visualizacao, apresente a prévia do AviHUB usando url_previa ou url_download. "
        "Quando relatorio_id for omitido, lista os relatórios do usuário neste espaço.")
    status["inputSchema"]["properties"]["relatorio_id"] = {
        "type":"string", "format":"uuid", "description":"ID do relatório retornado na solicitação de geração.",
    }
    specs[REPORT_STATUS] = status
    return specs


AVIHUB_TOOLS = avihub_tools()
for spec in AVIHUB_TOOLS.values():
    spec["inputSchema"]["properties"]["space_id"] = {"type": "string", "description": "Espaço autorizado. Se omitido, o HUB usa um espaço com AviHUB habilitado."}
    spec["description"] += " Se o resultado indicar vinculo_necessario, apresente a mensagem e o link url_vinculo ao cliente."
EQUIPMENT = AVIHUB_TOOLS["consultar_equipamento"]


def include_weight_report(tenant, workspace, agent_id, result):
    details = result.get("resultado")
    filters = details.get("filtros_peso_aves") if isinstance(details, dict) else None
    if not filters:
        return result
    context = {"conta":result["conta"]["id"], "unidade":result["unidade"]["id"], "formato":"pdf"}
    selected = {**context, **filters}
    portal_url = ORIGIN + reverse("avihub_reports", args=[tenant, workspace, agent_id])
    reading = {key:value for key,value in details.items() if key != "filtros_peso_aves"}
    if "eventos_peso_aves" in details:
        events = []
        for event in details["eventos_peso_aves"]:
            multi = {name:",".join(ids) for name, ids in event.get("filtros_multiplos", {}).items()}
            event_params = {**context, **event["filtros"], **multi}
            events.append({**event, "url_portal":portal_url + "?" + urlencode(event_params),
                           "parametros_pdf":{"space_id":str(workspace), **event_params}})
        reading["eventos_peso_aves"] = events
    return {
        **result,
        "resultado": reading,
        "relatorio_peso_aves": {
            "url_portal": portal_url + "?" + urlencode(selected),
            "parametros_pdf": {"space_id":str(workspace), **selected},
            "instrucao_para_ia": REPORT_ROUTING + " Preserve a conta, a unidade e os filtros autorizados. "
            "Para uma pesagem específica, use os filtros e o url_portal do evento correspondente em eventos_peso_aves. "
            "Se houver somente a URL da consulta, use o formulário para ajustar o período e os filtros ao evento antes de gerar.",
        },
    }


def with_report_resource(space, result):
    size = result.get("arquivo_tamanho")
    if (result.get("estado") == "pronto" and result.get("modo") == "arquivo" and result.get("fonte") in REPORT_FILE_SOURCES
            and result.get("arquivo_nome") and result.get("arquivo_tipo") and result.get("url_download")
            and (size is None or size <= MAX_MCP_REPORT_BYTES)):
        result = {**result, "arquivo_uri": f"{REPORT_RESOURCE_BASE}{space}/{result['relatorio_id']}"}
    return result


def tool_result(name, payload, protocol):
    blocks = [{"type":"text", "text":json.dumps(payload, ensure_ascii=False)}]
    if (name in (REPORT_GENERATE, REPORT_STATUS) and protocol != "2025-03-26" and payload.get("arquivo_uri")):
        blocks.append({"type":"resource_link", "uri":payload["arquivo_uri"], "name":payload["arquivo_nome"],
                       "mimeType":payload["arquivo_tipo"], "description":REPORT_FILE_SOURCES[payload["fonte"]],
                       "annotations":{"audience":["user"]}})
    return {"content":blocks}


def read_report_resource(link, ctx, uri):
    from . import avihub_reports

    match = REPORT_RESOURCE_FORMAT.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ValueError("Arquivo indisponível.")
    space, report_id = map(UUID, match.groups())
    if uri != f"{REPORT_RESOURCE_BASE}{space}/{report_id}":
        raise ValueError("Arquivo indisponível.")
    tenant, (workspace, agent_id) = avihub_entry(link, ctx, str(space))
    _, mime, data = avihub_reports.read_file(tenant, workspace, agent_id, link.user, report_id, link=link)
    if len(data) > MAX_MCP_REPORT_BYTES:
        raise ValueError("O arquivo está disponível pelo link autenticado do relatório.")
    return {"contents":[{"uri":uri, "mimeType":mime, "blob":b64encode(data).decode("ascii")}]}


def available_tools(link,ctx,can_reply):
    spaces = ctx.get("spaces", {link.space: ""})
    if getattr(link, "email_only", False):
        return mail_tools(link, ctx)
    result = [LIST_SPACES, TOOLS[0]]
    if ctx.get("guide_available"):
        result.append(PERSONAL_GUIDE)
    if link.space in spaces:
        result.extend(TOOLS[1:])
        if can_reply:
            result.append(REPLY)
    kinds = set()
    can_notify = False
    with connection.cursor() as cursor:
        for tenant, authorized in spaces_by_tenant(link, ctx).items():
            activate_tenant(cursor, tenant)
            cursor.execute("""SELECT DISTINCT kind,workspace_id FROM hub_tenancy.workspace_agents
                WHERE tenant_id=%s AND workspace_id=ANY(%s::uuid[]) AND active
                AND (kind IN ('notifications','library','avihub','knowledge','connector')
                     OR (kind='iaves' AND tenant_id=%s AND id=%s))""",
                [tenant,list(authorized),PESO_EXATO_TENANT_ID,IAVES_AGENT_ID])
            enabled = {}
            for kind, space in cursor.fetchall():
                enabled.setdefault(kind, []).append(space)
            if "library" in enabled and not LibraryAccess.objects.filter(
                    library__connection_id=tenant, space__in=enabled["library"]).exists():
                del enabled["library"]
            if not may_query_agent(link, ctx, tenant):
                enabled.pop("knowledge", None)
                enabled.pop("connector", None)
            kinds.update(enabled)
            can_notify |= "notifications" in enabled and may_notify(link, ctx, tenant)
        activate_tenant(cursor, link.tenant)
    if "iaves" in kinds:
        auto_memory = ctx.get("iaves_memory") and IAvesSettings.objects.filter(
            agent=IAVES_AGENT_ID, provider__in=iaves.PROVIDERS).exclude(model="").exclude(encrypted_key=b"").exists()
        result.append({**IAVES_QUERY, "annotations":{**IAVES_QUERY["annotations"], "readOnlyHint":not auto_memory}})
        if ctx.get("iaves_memory"):
            result.append(IAVES_REMEMBER)
    if "notifications" in kinds:
        result.extend([LIST_DESTINATIONS,DESCRIBE_NOTIFY])
        if can_notify:
            result.append(NOTIFY)
    if "library" in kinds:
        result.extend([SEARCH_NOTES, READ_NOTE])
    if getattr(link, "protocol", None) == "mcp" and "avihub" in kinds:
        result.extend(AVIHUB_TOOLS.values())
    if getattr(link, "all_tenants", False) and kinds.intersection({"knowledge", "connector"}):
        result.append(ASK_AGENT)
    result.extend(mail_tools(link, ctx))
    return result


def mail_tools(link, ctx):
    result = []
    if getattr(link, 'pk', None) and mailbox_service.authorized(link).exists():
        result.extend([LIST_MAIL, READ_MAIL, SEARCH_MAIL])
    if ctx.get("can_send_mail") and mailbox_service.sendable(link).exists():
        result.extend([SEND_MAIL, READ_SIGNATURE, EDIT_SIGNATURE])
    return result


def activate_tenant(cursor, tenant):
    cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])


def spaces_by_tenant(link, ctx):
    groups = {}
    tenants = ctx.get("space_tenants", {})
    for space, name in ctx.get("spaces", {link.space: ctx.get("workspace", "")}).items():
        groups.setdefault(tenants.get(space, link.tenant), {})[space] = name
    return groups


def may_query_agent(link, ctx, tenant):
    roles = ctx.get("tenant_roles", {link.tenant: None if ctx.get("viewer") else "member"})
    return roles.get(tenant) not in (None, "viewer")


def may_notify(link, ctx, tenant):
    return bool(ctx.get("can_notify") and may_query_agent(link, ctx, tenant))


def with_iaves_route(tenant, agent):
    if str(tenant) == str(PESO_EXATO_TENANT_ID) and agent["id"] == str(IAVES_AGENT_ID):
        return {**agent, "name":IAVES_NAME, "nome_formatado":IAVES_NAME_MARKDOWN,
                "ferramenta":IAVES_QUERY["name"],
                "instrucao_para_ia":f"Priorize consultar_iaves para dúvidas da cadeia avícola. {IAVES_DISPLAY}"}
    return agent


def agent_entries(link, ctx):
    agents = []
    with connection.cursor() as cursor:
        for tenant, spaces in spaces_by_tenant(link, ctx).items():
            activate_tenant(cursor, tenant)
            agents.extend(
                with_iaves_route(tenant, {"id":str(a["id"]),"name":a["name"],"description":a["description"],
                                         "space_id":str(space),"space_name":label,"tenant_id":str(tenant),"tipo":a["kind"],
                                         "url":ORIGIN + reverse("use_agent", args=[tenant, space, a["id"]])})
                for space,label in spaces.items() for a in catalog(cursor,tenant,space)
            )
        activate_tenant(cursor, link.tenant)
    return agents


def ticket_data(t):
    data = {"id":str(t.pk),"request":t.question,"state":t.state,"answer":t.answer,"created":t.created.isoformat()}
    if t.notice_agent:
        data.update(type="hub.agent.available", agent_id=str(t.notice_agent))
    return data


def call_tool(link,ctx,can_reply,params, *, record_usage=False):
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
    result = execute_tool(link, ctx, name, args)
    if record_usage and (spec["annotations"]["readOnlyHint"] or name == IAVES_QUERY["name"]):
        ConnectionUsage.record(link)
    return result


def ask_agent(link, ctx, tenant, space, args):
    if not may_query_agent(link, ctx, tenant):
        raise ValueError("Seu papel não permite consultar agentes neste espaço.")
    question = args["pergunta"].strip()
    request_id = UUID(args["request_id"])
    if not 1 <= len(question) <= 2000:
        raise ValueError("Informe uma consulta de até 2.000 caracteres.")
    try:
        with connection.cursor() as cursor:
            activate_tenant(cursor, tenant)
            agent = active_agent(cursor, tenant, space, UUID(args["agent_id"]))
            if agent["kind"] == "knowledge":
                return {"resposta":search_knowledge(agent["configuration"], question)}
            if agent["kind"] == "iaves":
                raise ValueError(f"Use consultar_iaves para consultar {IAVES_NAME} neste espaço.")
            if agent["kind"] != "connector":
                raise ValueError("Use a ferramenta correspondente ao tipo deste agente.")
            ref = agent["configuration"]
            config = available_connectors(cursor, tenant, space, secrets=ref == MODEL_REF).get(ref)
        if not config:
            raise ValueError("Conector indisponível neste espaço.")
        context = {"tenant_id":str(tenant),"workspace_id":str(space),"principal_id":str(ctx["principal"]),
                   "agent_id":str(agent["id"])}
        return {"resposta":query_connector(config, context, question, request_id)}
    except Http404:
        raise ValueError("Agente indisponível neste espaço.") from None
    except ConnectorError as exc:
        raise ValueError(str(exc)) from exc


def use_iaves(link, ctx, name, args):
    if name == IAVES_REMEMBER["name"] and not ctx.get("iaves_memory"):
        raise ValueError("A conexão não tem permissão para acessar a memória do IAves.")
    candidates = spaces_by_tenant(link, ctx).get(PESO_EXATO_TENANT_ID, {})
    details = None
    selected = UUID(args["space_id"]) if "space_id" in args else None
    reference = args.get("referencia_fontes")
    if reference is None and (question := args.get("pergunta", "")).startswith(iaves.SOURCE_REFERENCE_PREFIX):
        previous = question.removeprefix(iaves.SOURCE_REFERENCE_PREFIX).strip()
        if re.fullmatch(r"[a-zA-Z0-9_=-]{100,}", previous):
            reference = previous
    if reference is not None:
        source_space, details = iaves.read_source_reference(link.pk, reference)
        if selected is not None and selected != source_space:
            raise ValueError("Referência de fontes indisponível neste espaço.")
        selected = source_space
    if selected is not None:
        candidates = [selected] if selected in candidates else []
    with connection.cursor() as cursor:
        activate_tenant(cursor, PESO_EXATO_TENANT_ID)
        cursor.execute("""SELECT workspace_id FROM hub_tenancy.workspace_agents
            WHERE tenant_id=%s AND workspace_id=ANY(%s::uuid[]) AND id=%s AND active AND kind='iaves'
            ORDER BY (workspace_id=%s) DESC,workspace_id LIMIT 1""",
            [PESO_EXATO_TENANT_ID,list(candidates),IAVES_AGENT_ID,link.space])
        selected_space = cursor.fetchone()
        if not selected_space:
            raise ValueError("IAves indisponível neste espaço.")
        space = selected_space[0]
        agent = active_agent(cursor, PESO_EXATO_TENANT_ID, space, IAVES_AGENT_ID)
    if name == IAVES_REMEMBER["name"]:
        item = iaves.remember(link.user, agent, args["chave"], args["valor"])
        return iaves.without_repository_links({"estado":"memorizado","chave":item.key,"valor":item.value})
    if details is not None:
        return iaves.detailed_sources_response(details)
    question = args["pergunta"]
    if iaves.is_source_followup(question):
        return {"estado":"pergunta", "pergunta":"De qual consulta você quer ver as fontes?",
                "instrucao":"Passe a referencia_fontes que o IAves devolveu na consulta original, se estiver disponível. "
                            "Caso contrário, peça o assunto da consulta para buscar as notas."}
    details_only = iaves.wants_source_details(question)
    result = iaves.consult(link.user, {**ctx,"tenant":PESO_EXATO_TENANT_ID,"space":space}, agent, args,
                           allow_memory=bool(ctx.get("iaves_memory")), details_only=details_only)
    if details_only:
        return iaves.detailed_sources_response(iaves.source_details(result["fontes"]))
    return iaves.mcp_response(link.pk, space, result)


def avihub_entry(link, ctx, space_id):
    if space_id is None:
        groups = spaces_by_tenant(link, ctx)
    else:
        space = UUID(space_id)
        if space not in ctx.get("spaces", {link.space: ""}):
            raise ValueError("Espaço indisponível para esta conexão.")
        groups = {ctx.get("space_tenants", {}).get(space, link.tenant): [space]}
    # Use an active AviHUB entry visible to the current connection.
    entries = {}
    with connection.cursor() as cursor:
        for tenant, candidates in groups.items():
            activate_tenant(cursor, tenant)
            cursor.execute("""SELECT workspace_id,id FROM hub_tenancy.workspace_agents
                WHERE tenant_id=%s AND workspace_id=ANY(%s::uuid[]) AND active AND kind='avihub'
                ORDER BY (workspace_id=%s) DESC,workspace_id LIMIT 1""", [tenant,list(candidates),link.space])
            entry = cursor.fetchone()
            if entry:
                entries[tenant] = entry
        activate_tenant(cursor, link.tenant)
    if not entries:
        raise ValueError("AviHUB indisponível neste espaço.")
    if len(entries) > 1:
        raise ValueError("AviHUB disponível em mais de uma instituição. Informe o space_id desejado.")
    return next(iter(entries.items()))


def execute_tool(link, ctx, name, args):
    spaces = ctx.get("spaces", {link.space: ctx.get("workspace", "")})
    tenants = ctx.get("space_tenants", {})
    if name == "consultar_guia":
        guide = []
        with connection.cursor() as cursor:
            for tenant, authorized in spaces_by_tenant(link, ctx).items():
                activate_tenant(cursor, tenant)
                guide.extend(space_guide(cursor, tenant, authorized))
            activate_tenant(cursor, link.tenant)
        for group in guide:
            group["agents"] = [with_iaves_route(group["tenant"], agent) for agent in group["agents"]]
        return guide
    if name == "listar_espacos":
        return [{"space_id":str(key),"nome":label,"tenant_id":str(tenants.get(key, link.tenant))}
                for key,label in spaces.items()]
    if name in (IAVES_QUERY["name"], IAVES_REMEMBER["name"]):
        return use_iaves(link, ctx, name, args)
    if name in AVIHUB_TOOLS:
        tenant, entry = avihub_entry(link, ctx, args.get("space_id"))
        if name in AVIHUB_NOTICE_TOOLS:
            from . import avihub_notifications

            if name == AVIHUB_SEARCH_NOTICES and not args["busca"].strip():
                raise ValueError("Informe um termo para buscar nas notificações do AviHUB.")
            return avihub_notifications.query(tenant, *entry, link.user, args)
        if name in (REPORT_OPTIONS, REPORT_GENERATE, REPORT_STATUS):
            from . import avihub_reports

            workspace, agent_id = entry
            if name == REPORT_STATUS:
                return with_report_resource(workspace, avihub_reports.status(tenant, workspace, agent_id, link.user, args))
            operation = avihub_reports.options if name == REPORT_OPTIONS else avihub_reports.enqueue
            result = operation(tenant, workspace, agent_id, link.user, args, link=link, can_notify=may_notify(link, ctx, tenant))
            return with_report_resource(workspace, result) if name == REPORT_GENERATE else result
        if name == avihub_api.RESET_PASSWORD:
            agent = avihub.AviHubAgent.objects.filter(pk=entry[1], tenant=tenant).first()
            if not agent:
                raise ValueError("AviHUB indisponível neste espaço.")
            return {"mensagem": avihub.request_password_reset(agent, link.user, args["email"])}
        url = ORIGIN + reverse("use_agent", args=[tenant, *entry])
        result = avihub.query(tenant, link.user, args, name, link_url=url)
        if name == avihub_api.ACCOUNTS:
            return {**result, "url_notificacoes_avihub":ORIGIN + reverse("avihub_notifications", args=[tenant, *entry])}
        if name in BIRD_WEIGHT_READS:
            return include_weight_report(tenant, *entry, result)
        return result
    if name in SPACE_TOOLS:
        if "space_id" in args:
            space = UUID(args["space_id"])
            if space not in spaces:
                raise ValueError("Espaço indisponível para esta conexão.")
        elif len(spaces) == 1:
            space = next(iter(spaces))
        else:
            raise ValueError("Informe o space_id do espaço desejado. Consulte listar_espacos.")
        tenant = tenants.get(space, link.tenant)
    if name == ASK_AGENT["name"]:
        return ask_agent(link, ctx, tenant, space, args)
    if name in ("consultar_assinatura_email", "editar_assinatura_email"):
        return mailbox_service.signature(link, args)
    if name == "enviar_email":
        return mailbox_service.send(link, args)
    if name in ("listar_emails_novos", "ler_email", "buscar_emails"):
        return mailbox_service.query(link, name, args)
    if name in ("buscar_notas", "ler_nota"):
        with connection.cursor() as cursor:
            activate_tenant(cursor, tenant)
            return library.query(tenant, space, name, args)
    if name=="descrever_envio":
        return describe_notification()
    if name in ("listar_destinos","enviar_aviso"):
        agent = UUID(str(args["agent_id"]))
        with connection.cursor() as cursor:
            activate_tenant(cursor, tenant)
            if name=="enviar_aviso":
                if not may_notify(link, ctx, tenant):
                    raise ValueError("Envio de avisos indisponível neste espaço.")
                return notifications.send(cursor,link.user,{**ctx,"tenant":tenant,"space":space},agent,args,
                                          connection_name=link.name)
            if not any(a["id"]==agent and a["kind"]=="notifications" for a in catalog(cursor,tenant,space)):
                raise ValueError("Agente indisponível.")
            return [{"destino":str(d.pk),"nome":d.name,"canal":d.channel.channel}
                    for d in notifications.destinations(cursor,tenant,space,agent,ctx.get("spaces"))]
    if name=="list_agents":
        return agent_entries(link, ctx)
    with connection.cursor() as cursor:
        activate_tenant(cursor, link.tenant)
        if name=="list_tasks":
            return [ticket_data(t) for t in visible_tasks(link).filter(state="pending").order_by("created")[:50]]
        task = visible_tasks(link).select_for_update().filter(pk=UUID(str(args["task_id"]))).first()
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
    protocol = request.headers.get("MCP-Protocol-Version", VERSIONS[0])
    if protocol not in VERSIONS:
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
                          "capabilities":{"tools":{"listChanged":True},"events":{}},"serverInfo":dict(MCP_SERVER_INFO)}
                if email_only:
                    title = email_title(ctx["organization"])
                    result["serverInfo"].update(name=title, title=title)
                    del result["capabilities"]["events"]
                else:
                    result["capabilities"]["resources"] = {}
                    result["instructions"] = (f"{IAVES_ROUTING} {IAVES_DISPLAY} "
                                              "Para relatórios de Peso Aves solicitados na conversa, use as ferramentas nativas do AviHUB. "
                                              "Entregue o arquivo confirmado no resultado, com a origem informada em fonte. "
                                              "Se o catálogo do cliente não oferecer geração, use o formulário autenticado retornado na consulta.")
            elif method=="ping":
                result = {}
            elif method=="tools/list":
                result = discovery.list_tools(auth)
            elif method=="tools/call":
                try:
                    payload = call_tool(link,ctx,can_reply,params, record_usage=True)
                except avihub.SessionExpired as exc:
                    return fail(ident,-32602,str(exc))
            elif method=="resources/list" and not email_only:
                result = {"resources":[]}
            elif method=="resources/templates/list" and not email_only:
                result = {"resourceTemplates":[]}
            elif method=="resources/read" and not email_only:
                if not isinstance(params.get("uri"), str):
                    return fail(ident,-32602,"Informe a URI do arquivo de Peso Aves.")
                try:
                    result = read_report_resource(link, ctx, params["uri"])
                except (Http404, ValueError):
                    return fail(ident,-32002,"Arquivo indisponível para esta conexão. Use o link autenticado do relatório.")
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
        result = tool_result(params["name"], payload, protocol)
    if announce:
        return discovery.post_stream(ident, result)
    return success(ident,result)
