import json
import re
from uuid import UUID, uuid4

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext as translate_text
from django.utils.translation import gettext_lazy as tr
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from bridge import iaves
from bridge.models import IAVES_AGENT_ID, IAvesFeedback, IAvesSettings, LibraryAccess, PESO_EXATO_TENANT_ID
from model_provider import cipher
from space_views import active_agent, rows, space_context


class ModelConfiguration(forms.Form):
    provider = forms.ChoiceField(
        label=tr("Provedor"), required=False,
        choices=[("", tr("Canal para a minha IA")), *iaves.PROVIDERS.items()],
    )
    model = forms.RegexField(label=tr("Modelo"), required=False, max_length=100,
                             regex=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    token = forms.RegexField(label=tr("Token de API"), required=False, max_length=1024, regex=r"^[\x21-\x7e]+$",
                             widget=forms.PasswordInput(attrs={"autocomplete":"new-password"}))


@sensitive_variables("form", "token", "settings", "encrypted_key")
def configure(request, ctx, agent, record):
    if ctx["tenant"] != PESO_EXATO_TENANT_ID or agent != IAVES_AGENT_ID:
        raise Http404
    settings = IAvesSettings.objects.filter(agent=agent).first()
    configured = bool(settings and settings.provider and settings.model and settings.encrypted_key)
    initial = ({"provider":settings.provider, "model":settings.model} if configured else
               {"provider":iaves.DEFAULT_PROVIDER, "model":iaves.DEFAULT_MODEL})
    form = ModelConfiguration(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        provider, model, token = (form.cleaned_data[field] for field in ("provider", "model", "token"))
        if not provider or not model:
            if token:
                form.add_error("token", translate_text("Escolha um provedor e um modelo para usar o token."))
            else:
                IAvesSettings.objects.filter(agent=agent).delete()
                return redirect("institution_agent_edit", ctx["tenant"], agent)
        elif token or (configured and settings.provider == provider):
            encrypted_key = cipher().encrypt(token.encode()) if token else settings.encrypted_key
            IAvesSettings.objects.update_or_create(agent=agent, defaults={"provider":provider, "model":model,
                                                                         "encrypted_key":encrypted_key})
            return redirect("institution_agent_edit", ctx["tenant"], agent)
        else:
            form.add_error("token", translate_text("Informe o token para conectar este provedor."))
    return render(request, "iaves_settings.html", {**ctx,"agent":record,"form":form,"configured":configured,
                                                  "suggested_model":iaves.DEFAULT_MODEL}, status=400 if form.errors else 200)


def agent_context(cursor, user, tenant, space, agent):
    if tenant != PESO_EXATO_TENANT_ID or agent != IAVES_AGENT_ID:
        raise Http404
    ctx = space_context(cursor, user, tenant, space)
    record = active_agent(cursor, tenant, space, agent)
    if record["kind"] != "iaves":
        raise Http404
    return ctx, record


def dashboard_access(cursor, spaces):
    visible = [space["id"] for space in spaces if space["tenant"] == PESO_EXATO_TENANT_ID]
    if not visible:
        return None
    cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(PESO_EXATO_TENANT_ID)])
    cursor.execute("""SELECT a.workspace_id FROM hub_tenancy.workspace_agents a
        JOIN hub_tenancy.workspaces w ON (w.tenant_id,w.id)=(a.tenant_id,a.workspace_id)
        WHERE a.tenant_id=%s AND a.id=%s AND a.kind='iaves' AND a.active AND a.workspace_id=ANY(%s::uuid[])
        ORDER BY w.client_onboarding DESC,lower(w.name),w.id LIMIT 1""", [PESO_EXATO_TENANT_ID,IAVES_AGENT_ID,visible])
    row = cursor.fetchone()
    return {"tenant":PESO_EXATO_TENANT_ID,"space":row[0],"agent":IAVES_AGENT_ID} if row else None


def dashboard_library(cursor, spaces):
    visible = {(space["tenant"], space["id"]) for space in spaces}
    if not visible:
        return None
    grants = LibraryAccess.objects.filter(space__in={space for _, space in visible}).exclude(folders=[])
    libraries = {}
    for tenant, space, agent in grants.values_list("library__connection_id", "space", "library_id"):
        if (tenant, space) in visible:
            libraries.setdefault((tenant, agent), []).append(space)
    for tenant, agent in sorted(libraries, key=lambda key: (key[0] != PESO_EXATO_TENANT_ID, key)):
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])
        cursor.execute("""SELECT a.workspace_id FROM hub_tenancy.workspace_agents a
            JOIN hub_tenancy.workspaces w ON (w.tenant_id,w.id)=(a.tenant_id,a.workspace_id)
            WHERE a.tenant_id=%s AND a.id=%s AND a.kind='library' AND a.active AND a.workspace_id=ANY(%s::uuid[])
            ORDER BY w.client_onboarding DESC,lower(w.name),w.id LIMIT 1""", [tenant,agent,libraries[tenant,agent]])
        row = cursor.fetchone()
        if row:
            return {"tenant": tenant, "space": row[0], "agent": agent}
    return None


def decode_result(value):
    result = json.loads(value) if value else {}
    for source in result.get("fontes", []):
        legacy_url = source.pop("fonte", "")
        if not source.get("revisao") and (match := re.match(
                r"^https://github\.com/[^/]+/[^/]+/blob/([0-9a-f]{40})/", legacy_url)):
            source["revisao"] = match[1]
    return iaves.without_repository_links(result)


def feedback_eligible(result):
    if result.get("estado") == "resposta":
        return bool(result.get("resposta"))
    avihub = result.get("avihub") or {}
    return result.get("estado") == "contexto" and bool(result.get("fontes") or "dados" in avihub)


def save_feedback(request, cursor, scope, constraints):
    choice = request.POST.get("helpful")
    if choice not in ("yes", "no"):
        return HttpResponseBadRequest(translate_text("Escolha se a consulta resolveu sua dúvida."))
    try:
        identifier = UUID(request.POST.get("run_id", ""))
    except ValueError:
        raise Http404 from None
    cursor.execute(f"SELECT answer,created_at FROM hub_tenancy.agent_runs WHERE id=%s AND {constraints} AND status='done'",
                   [identifier,*scope])
    turn = cursor.fetchone()
    if not turn or not feedback_eligible(result := decode_result(turn[0])):
        raise Http404
    paths = list(dict.fromkeys(source["caminho"] for source in result.get("fontes", []) if source.get("caminho")))
    IAvesFeedback.objects.update_or_create(run=identifier, user=request.user, defaults={
        "helpful":choice == "yes", "consulted_at":turn[1], "source_paths":paths,
    })
    return redirect(reverse("use_agent", args=scope[:3]) + f"#consulta-{identifier}")


def saved_avihub(avihub):
    fields = ("url_vinculo", "url_avihub", "url_consulta", "mensagem", "periodo", "janela_horas")
    visible = {key:avihub[key] for key in fields if key in avihub}
    account = avihub.get("conta")
    if isinstance(account, dict):
        visible["conta"] = account_label(account)
    unit = avihub.get("unidade")
    if unit:
        visible["unidade"] = unit.get("nome", "") if isinstance(unit, dict) else unit
    for selector in ("contas", "unidades", "nucleos"):
        if avihub.get(selector):
            visible[selector] = [{key:item[key] for key in ("id", "nome", "ativa") if key in item}
                                 for item in avihub[selector]]
    for account in visible.get("contas", []):
        account["nome"] = account_label(account)
    data = avihub.get("resultado")
    if isinstance(data, dict) and not data.get("estado"):
        display_keys = ("total_exibido", "eventos", "proxima_pagina", "nucleos", "galpoes", "lotes")
        display = {key:data[key] for key in display_keys if key in data}
        if isinstance(data.get("equipamentos"), list):
            display["equipamentos"] = [equipment_details(item) for item in data["equipamentos"]]
        if display:
            visible["dados"] = display
    elif avihub.get("serie") and avihub.get("modelo"):
        visible["dados"] = {"equipamentos":[equipment_details(avihub)]}
    return visible


def account_label(account):
    name = account.get("nome")
    return name if name and "@" not in name else account.get("id", "")


def equipment_details(item):
    return {key:item[key] for key in ("serie", "modelo", "ultima_conexao", "localizacao") if key in item}


def saved_result(result, args):
    keys = ("estado", "resposta", "pergunta", "aviso", "base_local")
    visible = {key:result[key] for key in keys if key in result}
    source_fields = ("caminho", "revisao", "texto") if result["estado"] == "contexto" else ("caminho", "revisao")
    visible["fontes"] = [{key:source[key] for key in source_fields if key in source}
                         for source in result.get("fontes", [])]
    avihub = result.get("avihub")
    if not isinstance(avihub, dict):
        return visible
    visible["avihub"] = saved_avihub(avihub)
    if result.get("pergunta") and (not avihub or avihub.get("estado") in
                                   ("escolher_conta", "escolher_unidade", "escolher_nucleo", "indisponivel", "filtros_necessarios", "vinculo_necessario")):
        visible["retomar"] = {field:args[field] for field in iaves.AVIHUB_FIELDS if field in args}
        visible["retomar"]["avihub"] = iaves.avihub_operation(args["pergunta"], args)
    return visible


def source_url(tenant, space, agent, run_id=None):
    url = reverse("use_agent", args=[tenant, space, agent])
    if run_id:
        return f"{url}?fontes={run_id}#consulta-{run_id}"
    return f"{url}?fontes=indisponiveis"


def page(request, tenant, space, agent):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx, record = agent_context(cursor, request.user, tenant, space, agent)
        settings = IAvesSettings.objects.filter(agent=agent).values_list("provider","model","encrypted_key").first()
        configured = bool(settings and all(settings))
        scope = [tenant, space, agent, ctx["principal"]]
        constraints = "tenant_id=%s AND workspace_id=%s AND agent_id=%s AND user_id=%s"
        if request.method == "GET":
            source_request = request.GET.get("fontes", "")
            source_unavailable = source_request == "indisponiveis"
            try:
                source_run = UUID(source_request) if source_request and not source_unavailable else None
            except ValueError:
                raise Http404 from None
            cursor.execute(f"""SELECT id,question,answer,status,created_at,
                status='running' AND created_at < now()-interval '2 minutes' AS interrupted
                FROM hub_tenancy.agent_runs WHERE {constraints} ORDER BY created_at DESC,id DESC LIMIT 20""", scope)
            history = rows(cursor)
            feedback = dict(IAvesFeedback.objects.filter(user=request.user, run__in=[turn["id"] for turn in history])
                            .values_list("run", "helpful"))
            for turn in history:
                if turn["status"] == "done":
                    result = decode_result(turn["answer"])
                    turn["result"] = result
                    turn["feedback_eligible"] = feedback_eligible(result)
                    turn["feedback"] = feedback.get(turn["id"])
                    sources = result.get("fontes", [])
                    if sources:
                        if turn["id"] == source_run:
                            turn["source_details"] = iaves.source_details(sources)
                            source_run = None
                        for field in ("resposta", "pergunta", "aviso"):
                            if result.get(field):
                                result[field] = iaves.hide_source_metadata(result[field], sources)
                        for source in sources:
                            if source.get("texto"):
                                source["texto"] = iaves.hide_source_metadata(source["texto"], sources)
                elif turn["status"] == "failed":
                    turn["answer"] = iaves.hide_repository_links(turn["answer"])
            if source_run is not None:
                raise Http404
            latest = history[0] if history else {}
            show_avihub = bool(iaves.avihub_location(cursor,ctx))
            quick_actions = {"equipamentos": ("sem_comunicacao", tr("Quais equipamentos da minha unidade estão sem comunicação?")),
                             "pesagens": ("pesagens", tr("Quais pesagens foram registradas na minha unidade?"))}
            quick_action = quick_actions.get(request.GET.get("consulta")) if show_avihub else None
            resume = latest.get("result", {}).get("retomar", {})
            return render(request, "iaves_agent.html", {**ctx,"agent":record,"history":history,"configured":configured,
                                                       "latest":latest,"show_avihub":show_avihub,"resume":resume,
                                                       "source_label":iaves.SOURCE_LABEL,"source_unavailable":source_unavailable,
                                                       "operation":quick_action[0] if quick_action else resume.get("avihub"),
                                                       "quick_question":quick_action[1] if quick_action else "",
                                                       "request_id":uuid4()})
        if request.POST.get("action") == "feedback":
            return save_feedback(request, cursor, scope, constraints)
        question = request.POST.get("pergunta", "").strip()
        try:
            request_id = UUID(request.POST.get("request_id", ""))
        except ValueError:
            return HttpResponseBadRequest(translate_text("Solicitação inválida. Reabra o agente."))
        if not 1 <= len(question) <= 2000 or "\x00" in question:
            return HttpResponseBadRequest(translate_text("Informe uma consulta de até 2.000 caracteres."))
        if iaves.is_source_followup(question):
            cursor.execute(f"""SELECT id,answer FROM hub_tenancy.agent_runs
                WHERE {constraints} AND status='done' ORDER BY created_at DESC,id DESC LIMIT 1""", scope)
            previous = cursor.fetchone()
            run_id = previous[0] if previous and decode_result(previous[1]).get("fontes") else None
            return redirect(source_url(tenant, space, agent, run_id))
        details_only = iaves.wants_source_details(question)
        args = {field:value for field in ("avihub", *iaves.AVIHUB_FIELDS)
                if (value := request.POST.get(field, "").strip())}
        if any(len(value) > 200 or "\x00" in value for value in args.values()):
            return HttpResponseBadRequest(translate_text("Informe filtros do AviHUB de até 200 caracteres."))
        fingerprint = (*settings[:2], bytes(settings[2])) if configured else ()
        conversation = salted_hmac("hubia.iaves.history", repr(fingerprint), algorithm="sha256").hexdigest()
        cursor.execute("""INSERT INTO hub_tenancy.agent_runs
            (id,tenant_id,workspace_id,agent_id,user_id,question,status,conversation_key,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,'running',%s,statement_timestamp()) ON CONFLICT DO NOTHING RETURNING id""",
            [request_id,*scope,question,conversation])
        if cursor.fetchone() is None:
            return redirect("use_agent", tenant, space, agent)
        cursor.execute(f"""SELECT question,answer FROM hub_tenancy.agent_runs
            WHERE {constraints} AND conversation_key=%s AND status='done'
            ORDER BY created_at DESC,id DESC LIMIT 4""", [*scope,conversation])
        history = []
        for previous_question, answer in reversed(cursor.fetchall()):
            result = decode_result(answer)
            prior = result.get("resposta") or result.get("pergunta")
            if prior:
                history.append((previous_question, iaves.hide_source_metadata(prior, result.get("fontes", []))))
    args.update(pergunta=question, historico=history)
    try:
        result = iaves.consult(request.user, ctx, record, args, details_only=details_only)
        answer, status = json.dumps(saved_result(result,args), ensure_ascii=False), "done"
    except ValueError as exc:
        answer, status = iaves.hide_repository_links(str(exc)), "failed"
    with transaction.atomic(), connection.cursor() as cursor:
        agent_context(cursor, request.user, tenant, space, agent)
        cursor.execute(f"UPDATE hub_tenancy.agent_runs SET answer=%s,status=%s WHERE id=%s AND {constraints}",
                       [answer,status,request_id,*scope])
    if details_only and status == "done" and result.get("fontes"):
        return redirect(source_url(tenant, space, agent, request_id))
    return redirect("use_agent", tenant, space, agent)


@sensitive_post_parameters("value")
@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def memory(request, tenant, space, agent):
    error = ""
    with transaction.atomic(), connection.cursor() as cursor:
        ctx, record = agent_context(cursor, request.user, tenant, space, agent)
        items = iaves.memory_items(request.user, agent)
        if request.method == "POST":
            data = request.POST
            action = data.get("action")
            try:
                if action == "add":
                    iaves.remember(request.user, agent, data.get("key", ""), data.get("value", ""))
                elif action == "edit":
                    iaves.edit_memory(request.user, agent, data.get("item", ""), data.get("key", ""), data.get("value", ""))
                elif action == "delete":
                    if not items.filter(pk=UUID(data.get("item", ""))).delete()[0]:
                        raise Http404
                elif action == "clear" and data.get("confirm") == "on":
                    items.delete()
                else:
                    raise ValueError(translate_text("Confirme a limpeza da memória ou escolha uma ação válida."))
                return redirect("iaves_memory", tenant, space, agent)
            except ValueError as exc:
                error = str(exc)
        ctx.update(agent=record, items=list(items), error=error)
    return render(request, "iaves_memory.html", ctx, status=400 if error else 200)
