import json
import re
import secrets
import time

from django.utils.translation import gettext as translate_text
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from portal_identity import bind_account
from . import catalog_alerts, github_api
from .access import ORIGIN
from .agent_catalog import assigned, register_connector
from .library import PURPOSE, parse_folders
from .models import GitHubConnection, LibraryAccess, NoteLibrary


def institution(cursor, user, tenant):
    principal, owner = bind_account(cursor,user)
    if not principal:
        raise Http404
    if owner:
        cursor.execute("""SELECT organization FROM hub_admin.organization_directory()
                          WHERE tenant_id=%s AND status='ACTIVE' LIMIT 1""",[tenant])
    else:
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)",[str(tenant)])
        cursor.execute("""SELECT i.name FROM hub_tenancy.institutions i JOIN hub_tenancy.memberships m
                          ON i.tenant_id=m.tenant_id WHERE i.tenant_id=%s AND m.user_id=%s
                          AND m.role IN ('institution_owner','institution_admin') AND m.active""",[tenant,principal])
    row = cursor.fetchone()
    if not row:
        raise Http404
    return {"tenant":tenant,"organization":row[0],"is_owner":owner}


def start(request, tenant, login):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?",login):
        raise ValueError(translate_text("Informe o nome da organização no GitHub."))
    state = secrets.token_urlsafe(32)
    request.session["github_registration"] = {"tenant":str(tenant),"state":state,"login":login,"at":int(time.time())}
    manifest = {"name":"HUB IA "+login[:15]+" "+str(tenant)[:8],
                "url":ORIGIN, "public":False, "description":"Consulta institucional de repositórios selecionados.",
                "redirect_url":ORIGIN+reverse("github_registered",args=[tenant]),
                "setup_url":ORIGIN+reverse("github_connector",args=[tenant]),
                "default_permissions":{"contents":"read"},"default_events":[],
                "hook_attributes":{"url":f"{ORIGIN}/webhooks/github/","active":False}}
    return {"registration_url":f"https://github.com/organizations/{login}/settings/apps/new?state={state}",
            "manifest":json.dumps(manifest),"github_org":login}


def library_spaces(cursor, tenant, owner):
    if owner:
        cursor.execute("""SELECT workspace_id,workspace FROM hub_admin.organization_directory()
                          WHERE tenant_id=%s AND workspace_id IS NOT NULL""", [tenant])
    else:
        cursor.execute("SELECT id,name FROM hub_tenancy.workspaces WHERE tenant_id=%s ORDER BY lower(name)", [tenant])
    return dict(cursor.fetchall())


@never_cache
@login_required
@require_http_methods(["GET","POST"])
def connector(request, tenant):
    status = 200
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = institution(cursor,request.user,tenant)
        entries = GitHubConnection.objects.filter(tenant=tenant)
        conn = (entries.select_for_update() if request.method=="POST" else entries).first()
        ctx["conn"] = conn
        library = NoteLibrary.objects.filter(connection_id=tenant).first()
        ctx["library"] = library
        spaces = library_spaces(cursor, tenant, ctx["is_owner"]) if library else {}
        grants = dict(LibraryAccess.objects.filter(library=library).values_list("space", "folders")) if library else {}
        ctx["library_spaces"] = [{"id":s, "name":n, "folders":"\n".join(grants.get(s, []))} for s,n in spaces.items()]
        if request.method=="POST":
            action = request.POST.get("action")
            if action=="register" and conn is None:
                try:
                    ctx.update(start(request,tenant,request.POST.get("github_org","").strip()))
                except ValueError as exc:
                    ctx["error"],status = str(exc),400
            elif action == "create_library" and conn and not library:
                repo = next((r for r in conn.repositories if str(r["id"]) == request.POST.get("repo")), None)
                name = request.POST.get("name", "").strip()
                if conn.error or not repo or not 1 <= len(name) <= 120:
                    return HttpResponseBadRequest(translate_text("Selecione um repositório verificado e informe o nome do agente."))
                created = NoteLibrary.objects.create(connection=conn, name=name, repository_id=repo["id"])
                register_connector(cursor, tenant, created.id, name, "library", PURPOSE)
                return redirect("github_connector", tenant)
            elif action == "grant_folders" and library:
                space = next((key for key in spaces if str(key) == request.POST.get("space")), None)
                if space is None:
                    raise Http404
                try:
                    folders = parse_folders(request.POST.get("folders", ""))
                except ValueError as exc:
                    ctx["error"], status = str(exc), 400
                else:
                    if folders:
                        _, first = LibraryAccess.objects.update_or_create(library=library, space=space, defaults={"folders":folders})
                        if first and assigned(tenant, space, "library"):
                            catalog_alerts.announce(tenant, library.id, library.name, space=space, library_access=True)
                    else:
                        LibraryAccess.objects.filter(library=library, space=space).delete()
                        catalog_alerts.cancel_pending(tenant, space, library.id)
                    return redirect("github_connector", tenant)
            elif action=="test" and conn:
                try:
                    conn.installation_id,conn.repositories = github_api.inspect_repositories(conn,request.POST.get("repo",""))
                    conn.error = ""
                except github_api.GitHubError as exc:
                    conn.error = str(exc)
                    conn.repositories = []
                conn.checked_at = timezone.now()
                conn.save(update_fields=["installation_id","repositories","checked_at","error"])
                return redirect("github_connector",tenant)
            else:
                return HttpResponseBadRequest(translate_text("Ação indisponível."))
    return render(request,"github_connector.html",ctx,status=status)


@never_cache
@login_required
@require_GET
def registered(request, tenant):
    flow = request.session.get("github_registration",{})
    code = request.GET.get("code","")
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = institution(cursor,request.user,tenant)
        if (flow.get("tenant")!=str(tenant) or time.time()-flow.get("at",0)>3600
                or not secrets.compare_digest(flow.get("state","").encode(),request.GET.get("state","").encode())
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,250}",code)):
            return HttpResponseBadRequest(translate_text("Cadastro expirado ou inválido. Inicie a conexão novamente no HUB."))
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,615))",[str(tenant)])
        if not GitHubConnection.objects.filter(tenant=tenant).exists():
            try:
                fields = github_api.credentials(code,flow["login"])
            except github_api.GitHubError as exc:
                return render(request,"github_connector.html",{**ctx,"error":str(exc)},status=400)
            GitHubConnection.objects.create(tenant=tenant,organization=flow["login"],**fields)
        request.session.pop("github_registration",None)
    return redirect("github_connector",tenant)
