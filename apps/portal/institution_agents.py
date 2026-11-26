from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from agent_runtime import ConnectorError, connectors_for
from bridge import catalog_alerts
from bridge.github import institution, library_spaces
from bridge.models import NoteLibrary
from model_provider import MODEL_REF
from space_views import AgentForm, render_editor, rows


def lock_catalog(cursor, tenant):
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 915))", [str(tenant)])


def choices(cursor, tenant, space):
    cursor.execute("""SELECT a.id,a.name,a.description,a.kind,a.active,
            EXISTS(SELECT FROM hub_tenancy.agent_spaces s WHERE s.tenant_id=a.tenant_id
                   AND s.workspace_id=%s AND s.agent_id=a.id) AS assigned,
            count(*) OVER (PARTITION BY lower(a.name)) > 1 AS same_name
        FROM hub_tenancy.agents a WHERE a.tenant_id=%s AND a.workspace_id IS NULL AND NOT a.retired
        ORDER BY lower(a.name),a.id""", [space,tenant])
    return rows(cursor)


def save_selection(cursor, tenant, space, post):
    lock_catalog(cursor, tenant)
    agents = choices(cursor, tenant, space)
    available = {a["id"]: a for a in agents}
    try:
        selected = {UUID(value) for value in post.getlist("agents")}
        previous = {UUID(value) for value in post.getlist("previous")}
    except ValueError:
        return "Seleção de agentes inválida."
    current = {a["id"] for a in agents if a["assigned"]}
    if previous != current or not selected <= available.keys():
        return "A seleção mudou. Revise a lista atual antes de salvar."
    for ident in sorted(current - selected):
        cursor.execute("DELETE FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND workspace_id=%s AND agent_id=%s",
                       [tenant, space, ident])
        catalog_alerts.cancel_pending(tenant, space, ident)
    for ident in sorted(selected - current):
        cursor.execute("INSERT INTO hub_tenancy.agent_spaces(tenant_id,workspace_id,agent_id) VALUES (%s,%s,%s)",
                       [tenant, space, ident])
        if available[ident]["active"]:
            catalog_alerts.announce(tenant, ident, available[ident]["name"], space=space)
    return None


@sensitive_post_parameters("token")
@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def manage(request, tenant, agent=None):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = institution(cursor, request.user, tenant)
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])
        if request.method == "POST":
            lock_catalog(cursor, tenant)
        ctx["institution_editor"] = True
        if not agent and request.method == "GET" and "novo" not in request.GET:
            ctx["agents"] = choices(cursor, tenant, None)
            names = library_spaces(cursor, tenant, ctx["is_owner"])
            cursor.execute("SELECT agent_id,workspace_id FROM hub_tenancy.agent_spaces WHERE tenant_id=%s", [tenant])
            labels = {}
            for ident, space in cursor.fetchall():
                if space in names:
                    labels.setdefault(ident, []).append(names[space])
            for item in ctx["agents"]:
                item["spaces"] = sorted(labels.get(item["id"], []), key=str.casefold)
            cursor.execute("""SELECT id,name FROM hub_tenancy.agents WHERE tenant_id=%s
                AND workspace_id IS NULL AND retired AND kind <> 'guide' ORDER BY lower(name),id""", [tenant])
            ctx["retired_agents"] = rows(cursor)
            return render(request, "institution_agents.html", ctx)
        if request.method == "POST" and agent and request.POST.get("action") == "restore":
            cursor.execute("""UPDATE hub_tenancy.agents SET retired=false,active=true WHERE tenant_id=%s
                AND workspace_id IS NULL AND id=%s AND retired AND kind <> 'guide' RETURNING id""", [tenant,agent])
            if not cursor.fetchone():
                raise Http404
            return redirect("institution_agents", tenant)
        initial = {}
        if agent:
            cursor.execute("""SELECT name,kind,description,configuration,active FROM hub_tenancy.agents
                WHERE tenant_id=%s AND id=%s AND workspace_id IS NULL AND NOT retired""", [tenant,agent])
            found = rows(cursor)
            if not found:
                raise Http404
            initial = found[0]
        special = initial.get("kind") in ("avihub", "library")
        deleting = request.GET.get("excluir") == "1"
        if deleting and not agent:
            raise Http404
        if deleting and request.method == "GET":
            return render(request, "institution_agents.html", {**ctx, "deleting":initial})
        if request.method == "POST" and deleting:
            if request.POST.get("action") != "delete":
                raise Http404
            cursor.execute("UPDATE hub_tenancy.agents SET active=false,retired=true WHERE tenant_id=%s AND id=%s", [tenant,agent])
            cursor.execute("DELETE FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND agent_id=%s RETURNING workspace_id", [tenant,agent])
            for (space,) in cursor.fetchall():
                catalog_alerts.cancel_pending(tenant, space, agent)
            return redirect("institution_agents", tenant)
        if initial.get("kind") == "iaves":
            from iaves_portal import configure as configure_iaves

            return configure_iaves(request, ctx, agent, initial)
        connectors = {MODEL_REF}
        try:
            for space in library_spaces(cursor, tenant, ctx["is_owner"]):
                connectors.update(connectors_for(tenant,space))
        except ConnectorError as exc:
            ctx["warning"] = str(exc)
        if initial.get("kind") == "connector":
            connectors.add(initial["configuration"])
        form = AgentForm(request.POST if request.method=="POST" else None, initial=initial, connectors=connectors)
        if special:
            form.fields["kind"].choices = [(initial["kind"], "AviHUB" if initial["kind"]=="avihub" else "Biblioteca GitHub")]
            form.fields["kind"].disabled = True
        if request.method=="POST" and form.is_valid():
            data = form.cleaned_data
            values = [data[k] for k in ("name","kind","description","configuration","active")]
            if agent:
                cursor.execute("""UPDATE hub_tenancy.agents SET (name,kind,description,configuration,active)=(%s,%s,%s,%s,%s)
                    WHERE tenant_id=%s AND id=%s""", [*values,tenant,agent])
                if initial["kind"] == "library":
                    NoteLibrary.objects.filter(connection_id=tenant, pk=agent).update(name=data["name"])
                if initial["active"] != data["active"]:
                    cursor.execute("SELECT workspace_id FROM hub_tenancy.agent_spaces WHERE tenant_id=%s AND agent_id=%s", [tenant,agent])
                    for (space,) in cursor.fetchall():
                        if data["active"]:
                            catalog_alerts.announce(tenant, agent, data["name"], space=space)
                        else:
                            catalog_alerts.cancel_pending(tenant, space, agent)
            else:
                cursor.execute("""INSERT INTO hub_tenancy.agents(name,kind,description,configuration,active,tenant_id)
                    VALUES (%s,%s,%s,%s,%s,%s)""", [*values,tenant])
            return redirect("institution_agents", tenant)
        return render_editor(request, ctx, form, agent, initial, connectors)
