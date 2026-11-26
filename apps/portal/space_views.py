import json
from uuid import UUID, uuid4

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import connection, transaction, IntegrityError, DatabaseError
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from model_provider import MODEL_REF, ModelForm, available_connectors, save_model
from agent_runtime import ConnectorError, https_url, query_connector, search_knowledge
from bridge import mail_access
from portal_identity import bind_account
from bridge import avihub, catalog_alerts, library
from bridge.models import CatalogAlerts, LibraryAccess


class AgentForm(forms.Form):
    config_inputs = {"knowledge": "knowledge_text", "external": "external_url", "connector": "connector_id"}

    name = forms.CharField(label="Nome", max_length=120)
    description = forms.CharField(label="Descrição", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    kind = forms.ChoiceField(label="Funcionamento", choices=[
        ("notifications","Notificações por canal"), ("knowledge","Consulta à base de conhecimento"),
        ("external","Abrir portal externo"), ("connector","Consulta a uma API conectada")])
    configuration = forms.CharField(label="Configuração", required=False, max_length=16000)
    active = forms.BooleanField(label="Disponível aos membros", required=False, initial=True)

    def __init__(self, data=None, *, connectors, **kwargs):
        if data is not None:
            # Existing clients still submit the canonical configuration field.
            field = self.config_inputs.get(data.get("kind"))
            if field and field in data:
                data = data.copy()
                data["configuration"] = data[field]
        super().__init__(data, **kwargs)
        self.connectors = connectors

    def clean(self):
        data = super().clean()
        kind, value = data.get("kind"), data.get("configuration", "")
        if kind == "external":
            try:
                https_url(value)
            except (ValidationError, ValueError):
                self.add_error("configuration", "Informe uma URL HTTPS válida e sem credenciais.")
        elif kind == "knowledge" and not value:
            self.add_error("configuration", "Inclua ao menos um trecho para pesquisa.")
        elif kind == "connector" and value not in self.connectors:
            self.add_error("configuration", "Escolha um conector configurado para este espaço.")
        elif kind == "notifications":
            data["configuration"] = ""
        return data


def render_editor(request, ctx, form, agent, initial, connectors):
    for kind, field in form.config_inputs.items():
        ctx[field] = (form["configuration"].value() or "") if form["kind"].value() == kind else ""
    ctx.update(form=form, edit=agent, original=initial, connectors=sorted(connectors), model_ref=MODEL_REF)
    return render(request, "agent_editor.html", ctx, status=400 if form.errors else 200)


def space_context(cursor, user, tenant, space, management=False):
    principal, is_owner = bind_account(cursor, user)
    if not principal:
        raise Http404
    cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])
    if management:
        cursor.execute("SELECT * FROM hub_admin.space_management(%s,%s)", [tenant,space])
    else:
        cursor.execute("""SELECT coalesce(i.name,'Espaço pessoal'), w.name FROM hub_tenancy.workspaces w
            LEFT JOIN hub_tenancy.institutions i ON i.tenant_id=w.tenant_id
            WHERE w.tenant_id=%s AND w.id=%s""", [tenant,space])
    row = cursor.fetchone()
    if not row:
        raise Http404
    return {"tenant":tenant,"space":space,"organization":row[0],"workspace":row[1],"principal":principal,"is_owner":is_owner}


def rows(cursor):
    keys = [column.name for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def catalog(cursor, tenant, space, include_inactive=False):
    cursor.execute("""SELECT id,name,description,kind,active FROM hub_tenancy.workspace_agents
        WHERE tenant_id=%s AND workspace_id=%s AND (active OR %s) ORDER BY lower(name),id""",
        [tenant,space,include_inactive])
    return rows(cursor)


@never_cache
@login_required
@require_GET
def workspace(request, tenant, space):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space)
        ctx["agents"] = catalog(cursor,tenant,space)
        cursor.execute("SELECT hub_admin.can_manage_space(%s,%s)",[tenant,space])
        ctx["manager"] = cursor.fetchone()[0]
        ctx["internal_mail"] = mail_access.allowed(cursor, tenant)
        cursor.execute("SELECT EXISTS(SELECT FROM hub_tenancy.institutions WHERE tenant_id=%s)",[tenant])
        ctx["institutional"] = cursor.fetchone()[0]
    return render(request,"workspace.html",ctx)


@never_cache
@login_required
@require_http_methods(["GET","POST"])
def manage_agents(request, tenant, space, agent=None):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space,management=True)
        cursor.execute("SELECT EXISTS(SELECT FROM hub_tenancy.workspaces WHERE tenant_id=%s AND id=%s)",[tenant,space])
        ctx["can_open"] = cursor.fetchone()[0]
        cursor.execute("SELECT EXISTS(SELECT FROM hub_tenancy.personal_spaces WHERE tenant_id=%s)",[tenant])
        ctx["institutional"] = not cursor.fetchone()[0]
        alerts = CatalogAlerts.objects.filter(tenant=tenant, space=space)
        if not agent and request.method=="POST" and request.POST.get("action")=="catalog_alerts":
            if request.POST.get("enabled")=="on":
                CatalogAlerts.objects.get_or_create(tenant=tenant, space=space)
            else:
                alerts.delete()
                catalog_alerts.cancel_pending(tenant, space)
            return redirect("manage_agents", tenant, space)
        ctx["catalog_alerts_enabled"] = alerts.exists()
        if ctx["institutional"]:
            from institution_agents import choices, save_selection
            if request.method == "POST":
                if request.POST.get("action") != "select_agents" or agent:
                    return HttpResponseBadRequest("Edite o cadastro no catálogo da instituição.")
                ctx["error"] = save_selection(cursor, tenant, space, request.POST)
                if not ctx["error"]:
                    return redirect("manage_agents", tenant, space)
            if agent:
                return redirect("institution_agent_edit", tenant, agent)
            if request.GET.get("novo") == "1":
                return redirect("institution_agents", tenant)
            ctx["choices"] = choices(cursor, tenant, space)
        initial = {}
        if agent:
            cursor.execute("""SELECT name,kind,description,configuration,active FROM hub_tenancy.agents
                WHERE tenant_id=%s AND workspace_id=%s AND id=%s AND NOT retired""" +
                (" FOR UPDATE" if request.method=="POST" else ""), [tenant,space,agent])
            existing = rows(cursor)
            if not existing:
                raise Http404
            initial = existing[0]
        notice_key = f"agent_saved_{space}"
        editor = agent or request.method == "POST" or request.GET.get("novo") == "1"
        if editor and not ctx["institutional"]:
            try:
                connectors = available_connectors(cursor,tenant,space)
            except ConnectorError as exc:
                connectors = {}
                ctx["warning"] = str(exc)
            form = AgentForm(request.POST if request.method=="POST" else None,initial=initial,connectors=connectors)
            if request.method=="POST" and form.is_valid():
                values = [form.cleaned_data[k] for k in ("name","kind","description","configuration","active")]
                try:
                    with transaction.atomic():
                        if agent:
                            cursor.execute("""UPDATE hub_tenancy.agents SET (name,kind,description,configuration,active)=(%s,%s,%s,%s,%s)
                                WHERE tenant_id=%s AND workspace_id=%s AND id=%s RETURNING id""",[*values,tenant,space,agent])
                            if not cursor.fetchone():
                                raise Http404
                        else:
                            cursor.execute("""INSERT INTO hub_tenancy.agents (name,kind,description,configuration,active,tenant_id,workspace_id)
                                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",[*values,tenant,space])
                            agent = cursor.fetchone()[0]
                        if form.cleaned_data["active"] and not initial.get("active"):
                            catalog_alerts.announce(tenant, agent, values[0], space=space)
                        elif initial.get("active") and not form.cleaned_data["active"]:
                            catalog_alerts.cancel_pending(tenant, space, agent)
                    request.session[notice_key] = {"id": str(agent), "name": values[0], "created": not initial}
                    return redirect("manage_agents",tenant,space)
                except IntegrityError as exc:
                    if exc.__cause__.sqlstate != "23505":
                        raise
                    form.add_error("name","Já existe um agente com esse nome no espaço.")
            return render_editor(request, ctx, form, agent, initial, connectors)
        if not ctx["institutional"]:
            agents = catalog(cursor, tenant, space, True)
            kinds = dict(AgentForm.base_fields["kind"].choices)
            for item in agents:
                item["kind_label"] = kinds.get(item["kind"], "Institucional")
            ctx.update(agents=agents, saved=request.session.pop(notice_key, None))
        cursor.execute("SELECT * FROM hub_admin.space_people(%s,%s)",[tenant,space])
        ctx["people"] = rows(cursor)
        labels = {"institution_owner":"Responsável","institution_admin":"Administrador institucional",
                  "personal_owner":"Titular","member":"Membro","viewer":"Leitor"}
        for person in ctx["people"]:
            person["label"] = labels.get(person["role"],person["role"])
    return render(request,"manage_agents.html",ctx,status=400 if ctx.get("error") else 200)


def active_agent(cursor, tenant, space, agent):
    cursor.execute("""SELECT id,name,description,kind,configuration FROM hub_tenancy.workspace_agents
        WHERE tenant_id=%s AND workspace_id=%s AND id=%s AND active""",[tenant,space,agent])
    found = rows(cursor)
    if not found:
        raise Http404
    return found[0]


@never_cache
@login_required
@require_http_methods(["GET","POST"])
@sensitive_post_parameters("password", "code", "token")
def use_agent(request, tenant, space, agent):
    remote = None
    history = []
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space)
        record = active_agent(cursor,tenant,space,agent)
        if record["kind"] == "avihub":
            equipment = avihub.AviHubAgent.objects.filter(tenant=tenant, pk=agent).first()
            if not equipment:
                raise Http404
            ctx["agent_name"] = record["name"]
            return avihub.page(request, ctx, equipment)
        if record["kind"] == "library":
            ctx.update(agent=record, enabled=LibraryAccess.objects.filter(library_id=agent, space=space).exists())
            if request.method == "POST":
                action = request.POST.get("action")
                if action not in ("buscar_notas", "ler_nota"):
                    return HttpResponseBadRequest("Ação indisponível.")
                try:
                    ctx["result"] = library.query(tenant, space, action, request.POST)
                except ValueError as exc:
                    ctx["error"] = str(exc)
            return render(request, "library_agent.html", ctx)
        ctx["agent"] = record
        if record["kind"]=="notifications":
            from bridge.notifications import channels, destinations, send
            if request.method=="POST":
                try:
                    send(cursor,request.user,ctx,agent,request.POST)
                except ValueError as exc:
                    return HttpResponseBadRequest(str(exc))
                return redirect("use_agent",tenant,space,agent)
            choices = list(destinations(cursor,tenant,space,agent))
            from notification_channels import CHANNELS
            ctx["targets"] = choices
            enabled = channels(tenant,space,agent).filter(enabled=True).values_list("channel",flat=True)
            ctx["channels"] = {key: CHANNELS[key][0] for key in enabled}
        if request.method=="GET":
            cursor.execute("SELECT hub_admin.can_manage_space(%s,%s)",[tenant,space])
            ctx["manager"] = cursor.fetchone()[0]
            cursor.execute("SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s",[tenant])
            ctx["viewer"] = cursor.fetchone()[0]=="viewer"
            cursor.execute("""SELECT question,answer,status,created_at,
                status='running' AND created_at < now()-interval '2 minutes' AS interrupted
                FROM hub_tenancy.agent_runs WHERE tenant_id=%s AND workspace_id=%s AND agent_id=%s AND user_id=%s
                ORDER BY created_at DESC,id DESC LIMIT 50""",[tenant,space,agent,ctx["principal"]])
            ctx.update(history=rows(cursor),request_id=uuid4())
            return render(request,"agent.html",ctx)
        cursor.execute("SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s",[tenant])
        if cursor.fetchone()[0] == "viewer":
            return HttpResponseForbidden("Seu papel de leitor permite consultar o catálogo e o histórico.")
        if record["kind"]=="external":
            return HttpResponseBadRequest("Abra o portal externo pelo link do agente.")
        question = request.POST.get("question","").strip()
        try:
            run = UUID(request.POST.get("request_id",""))
        except ValueError:
            return HttpResponseBadRequest("Solicitação inválida. Reabra o agente.")
        if not 1 <= len(question) <= 2000:
            return HttpResponseBadRequest("Informe uma consulta de até 2.000 caracteres.")
        cursor.execute("""INSERT INTO hub_tenancy.agent_runs (id,tenant_id,workspace_id,agent_id,user_id,question,status)
            VALUES (%s,%s,%s,%s,%s,%s,'running') ON CONFLICT DO NOTHING RETURNING id""",
            [run,tenant,space,agent,ctx["principal"],question])
        if not cursor.fetchone():
            return redirect("use_agent",tenant,space,agent)
        if record["kind"]=="connector":
            try:
                remote = available_connectors(cursor,tenant,space,secrets=record["configuration"]==MODEL_REF).get(record["configuration"])
                if isinstance(remote,dict) and remote.get("type") == "chat_completions":
                    # Uma mudança no destino, chave ou instruções inicia um novo contexto.
                    conversation = salted_hmac("hubia.agent.history",json.dumps(remote,sort_keys=True),algorithm="sha256").hexdigest()
                    cursor.execute("UPDATE hub_tenancy.agent_runs SET conversation_key=%s WHERE id=%s",[conversation,run])
                    cursor.execute("""SELECT question,answer FROM hub_tenancy.agent_runs
                        WHERE tenant_id=%s AND workspace_id=%s AND agent_id=%s AND user_id=%s AND status='done' AND conversation_key=%s
                        ORDER BY created_at DESC,id DESC LIMIT 4""",[tenant,space,agent,ctx["principal"],conversation])
                    history = list(reversed(cursor.fetchall()))
                if not isinstance(remote,dict) or not remote:
                    remote = None
                    raise ConnectorError("Conector indisponível neste espaço.")
            except ConnectorError as exc:
                cursor.execute("UPDATE hub_tenancy.agent_runs SET answer=%s,status='failed' WHERE id=%s",[str(exc),run])
        else:
            answer = search_knowledge(record["configuration"],question)
            cursor.execute("UPDATE hub_tenancy.agent_runs SET answer=%s,status='done' WHERE id=%s",[answer,run])
    if remote:
        context = {"tenant_id":str(tenant),"workspace_id":str(space),"principal_id":str(ctx["principal"]),"agent_id":str(agent)}
        try:
            answer = query_connector(remote,context,question,run,history)
            status = "done"
        except ConnectorError as exc:
            answer,status = str(exc),"failed"
        with transaction.atomic(), connection.cursor() as cursor:
            space_context(cursor,request.user,tenant,space)
            active_agent(cursor,tenant,space,agent)
            cursor.execute("UPDATE hub_tenancy.agent_runs SET answer=%s,status=%s WHERE id=%s",[answer,status,run])
    return redirect("use_agent",tenant,space,agent)


@sensitive_post_parameters("api_key")
@never_cache
@login_required
@require_http_methods(["GET","POST"])
def configure_model(request, tenant, space):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space,management=True)
        cursor.execute("SELECT model,instructions FROM hub_tenancy.workspace_models WHERE tenant_id=%s AND workspace_id=%s",
                       [tenant,space])
        current = rows(cursor)
        form = ModelForm(request.POST if request.method=="POST" else None,initial=current[0] if current else {})
        cursor.execute("SELECT EXISTS (SELECT FROM hub_tenancy.personal_spaces WHERE tenant_id=%s)", [tenant])
        personal_space = cursor.fetchone()[0]
        if not personal_space:
            form.fields.pop("install_agent")
        if request.method=="POST":
            if request.POST.get("action")=="disconnect":
                cursor.execute("DELETE FROM hub_tenancy.workspace_models WHERE tenant_id=%s AND workspace_id=%s",[tenant,space])
                return redirect("configure_model",tenant,space)
            if form.is_valid():
                if save_model(cursor,tenant,space,form.cleaned_data):
                    if personal_space and form.cleaned_data["install_agent"]:
                        cursor.execute("""INSERT INTO hub_tenancy.agents(tenant_id,workspace_id,name,kind,description,configuration)
                            VALUES (%s,%s,'Assistente de IA','connector','Converse com o modelo configurado para este espaço.',%s)
                            ON CONFLICT (tenant_id,workspace_id,name) DO NOTHING RETURNING id""",[tenant,space,MODEL_REF])
                        created = cursor.fetchone()
                        if created:
                            catalog_alerts.announce(tenant, created[0], "Assistente de IA", space=space)
                    return redirect("configure_model",tenant,space)
                form.add_error("api_key","Informe a chave para configurar o modelo pela primeira vez.")
        ctx.update(form=form,configured=bool(current),reference=MODEL_REF)
    return render(request,"model.html",ctx,status=400 if form.errors else 200)

@never_cache
@login_required
@require_http_methods(["POST"])
def manage_member(request, tenant, space):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space,management=True)
        username = request.POST.get("username","").strip()
        pk = get_user_model().objects.filter(username=username,is_active=True).values_list("pk",flat=True).first()
        error = "Usuário ativo não encontrado."
        if pk is not None:
            try:
                with transaction.atomic():
                    cursor.execute("SELECT hub_admin.manage_member(%s,%s,hub_admin.portal_principal(%s),%s,%s)",
                                   [tenant,space,str(pk),request.POST.get("action"),request.POST.get("role")])
                return redirect("manage_agents",tenant,space)
            except DatabaseError as exc:
                if exc.__cause__.sqlstate not in {"22023","42501"}:
                    raise
                error = exc.__cause__.diag.message_primary
    return render(request,"member_error.html",{**ctx,"error":error},status=400)
