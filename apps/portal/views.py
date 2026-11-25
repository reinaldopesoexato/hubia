from uuid import UUID

from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.db import DatabaseError, connection, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.utils.text import slugify


from bridge import a2a, github, mail_views, mcp, oauth
from bridge.workspaces import manage as ai_workspaces
from bridge.portal import integrations
from bridge.models import CatalogAlerts, LibraryAccess, Link, NotificationChannel
from bridge.catalog_alerts import cancel_pending
from dashboard import agent_directory, overview
from account_admin import people
from portal_identity import bind_account
from bridge.notifications import configure as notification_config
from space_views import configure_model, manage_agents, manage_member, use_agent, workspace


class PortalLogin(LoginView):
    template_name = "login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        source = request.META.get("HTTP_X_REAL_IP", request.META["REMOTE_ADDR"])
        key = salted_hmac("portal-login", source).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM login_limits WHERE started_at < now() - interval '5 minutes'")
            cursor.execute("""
                INSERT INTO login_limits (source) VALUES (%s)
                ON CONFLICT (source) DO UPDATE SET attempts = login_limits.attempts + 1
                WHERE login_limits.attempts < 20 RETURNING attempts
            """, [key])
            if not cursor.fetchone():
                return HttpResponse("Muitas tentativas. Aguarde cinco minutos.", status=429,
                                    headers={"Retry-After": "300"})
        return super().post(request, *args, **kwargs)


@never_cache
@login_required
def account(request):
    period = 30 if request.GET.get("dias") == "30" else 7
    with transaction.atomic(), connection.cursor() as cursor:
        principal, is_owner = bind_account(cursor, request.user)
        if principal is None:
            logout(request)
            return HttpResponse("O acesso desta conta está desativado.", status=403)
        context = overview(cursor, principal, period)
    return render(request, "account.html", {**context, "is_owner": is_owner})


@never_cache
@login_required
@require_http_methods(["GET"])
def active_agents(request):
    with transaction.atomic(), connection.cursor() as cursor:
        principal, is_owner = bind_account(cursor, request.user)
        if principal is None:
            logout(request)
            return HttpResponse("O acesso desta conta está desativado.", status=403)
        groups = agent_directory(cursor)
    return render(request, "active_agents.html", {"groups":groups, "is_owner":is_owner})


def apply_admin_form(cursor, form):
    action = form.get("action")
    actions = {"create_org", "rename_org", "activate", "suspend", "create_space",
               "rename_space", "grant_space", "revoke_space"}
    if action not in actions:
        raise ValueError("Operação inválida.")
    try:
        tenant = None if action == "create_org" else UUID(form.get("tenant", ""))
        workspace = UUID(form.get("workspace", "")) if action in {"rename_space", "grant_space", "revoke_space"} else None
    except ValueError:
        raise ValueError("Selecione uma instituição e um espaço válidos.") from None
    name = form.get("name", "").strip()
    target = None
    username = form.get("username", "").strip()
    if action in {"grant_space", "revoke_space"} or (action == "create_org" and username):
        account_id = get_user_model().objects.filter(username=username, is_active=True).values_list("pk", flat=True).first()
        if account_id is not None:
            cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(account_id)])
            target = cursor.fetchone()[0]
        if target is None:
            raise ValueError("Usuário ativo não encontrado.")
    slug = slugify(name).replace("_", "-")[:80].rstrip("-")
    cursor.execute("SELECT hub_admin.manage_organization(%s,%s,%s,%s,%s,%s)",
                   [action, tenant, workspace, name, slug, target])


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def administration(request):
    error = None
    groups = {}
    with transaction.atomic(), connection.cursor() as cursor:
        _, is_owner = bind_account(cursor, request.user)
        if not is_owner:
            return HttpResponse("A administração exige uma conta de administrador geral.", status=403)
        if request.method == "POST":
            try:
                with transaction.atomic():
                    apply_admin_form(cursor, request.POST)
                return redirect("administration")
            except ValueError as exc:
                error = str(exc)
            except DatabaseError as exc:
                code = getattr(exc.__cause__, "sqlstate", None)
                if code == "23505":
                    error = "Já existe um cadastro com esse identificador."
                elif code == "22023":
                    error = exc.__cause__.diag.message_primary
                else:
                    raise
        cursor.execute("SELECT * FROM hub_admin.organization_directory()")
        for tenant, name, status, space, space_name in cursor.fetchall():
            org = groups.setdefault(tenant, {"id": tenant, "name": name, "status": status, "spaces": []})
            if space:
                org["spaces"].append({"id": space, "name": space_name})
        cursor.execute("SELECT tenant_id, workspace_id, workspace, deleted_at FROM hub_admin.workspace_inventory() WHERE deleted_at IS NOT NULL")
        for tenant, space, name, deleted_at in cursor.fetchall():
            groups[tenant].setdefault("trash", []).append({"id": space, "name": name, "deleted_at": deleted_at})
    return render(request, "administration.html", {"organizations": groups.values(), "error": error, "is_owner": True,
                  "active_orgs": sum(org["status"] == "ACTIVE" for org in groups.values()),
                  "space_count": sum(len(org["spaces"]) for org in groups.values())},
                  status=400 if error else 200)


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def workspace_settings(request, tenant, space):
    ctx = {"tenant":tenant, "space":space, "is_owner":True}
    with transaction.atomic(), connection.cursor() as cursor:
        _, owner = bind_account(cursor, request.user)
        if not owner:
            raise Http404
        cursor.execute("""SELECT workspace,principal,deleted_at,organization,status
            FROM hub_admin.workspace_inventory() WHERE tenant_id=%s AND workspace_id=%s""", [tenant,space])
        row = cursor.fetchone()
        if not row or row[4] != "ACTIVE":
            raise Http404
        name, principal, deleted_at, organization, _ = row
        ctx.update(workspace=name, principal_space=principal, deleted_at=deleted_at, organization=organization)
        if request.method == "POST":
            action = request.POST.get("action")
            if action not in ("rename_space", "trash_space", "restore_space"):
                return HttpResponse("Operação inválida.", status=400)
            label = request.POST.get("name", "").strip()
            try:
                with transaction.atomic():
                    cursor.execute("SELECT hub_admin.manage_organization(%s,%s,%s,%s,NULL,NULL)",
                                   [action,tenant,space,label])
                    if action in ("trash_space","restore_space"):
                        cancel_pending(tenant, space)
                if action == "trash_space":
                    return redirect("administration")
                return redirect("workspace_settings", tenant, space)
            except DatabaseError as exc:
                if getattr(exc.__cause__, "sqlstate", None) != "22023":
                    raise
                ctx["error"] = exc.__cause__.diag.message_primary
        if not deleted_at:
            cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])
            cursor.execute("""SELECT (SELECT count(*) FROM hub_admin.space_people(%s,%s)),
                (SELECT count(*) FROM hub_tenancy.agents WHERE tenant_id=%s AND workspace_id=%s)""",
                [tenant,space,tenant,space])
            members, agents = cursor.fetchone()
            ctx["impact"] = [
                ("Membros ativos", members), ("Agentes próprios", agents),
                ("Conexões", Link.objects.filter(tenant=tenant,space=space).count()),
                ("Canais de notificação", NotificationChannel.objects.filter(tenant=tenant,space=space).count()),
            ]
            ctx["folders"] = LibraryAccess.objects.filter(library__connection_id=tenant,space=space).values_list("folders",flat=True).first() or []
            ctx["catalog_alerts"] = CatalogAlerts.objects.filter(tenant=tenant,space=space).exists()
    return render(request, "workspace_settings.html", ctx, status=400 if "error" in ctx else 200)


space_path = "conta/espacos/<uuid:tenant>/<uuid:space>/"
urlpatterns = [
    path("conta/minhas-ias/espacos/",ai_workspaces,name="ai_workspaces"),
    path(".well-known/agent-card.json",a2a.card),
    path(".well-known/oauth-protected-resource/conta/mcp",oauth.protected_metadata),
    path(".well-known/oauth-protected-resource",oauth.protected_metadata),
    path(".well-known/oauth-authorization-server",oauth.server_metadata),
    path("conta/oauth/authorize/",oauth.Consent.as_view(),name="bridge_authorize"),
    path("conta/oauth/token/",oauth.token),
    path("conta/oauth/register/",oauth.register),
    path("conta/mcp",mcp.endpoint),
    path("conta/mcp/email", mcp.endpoint, {"email_only":True}, name="email_mcp"),
    path(".well-known/oauth-protected-resource/conta/mcp/email", oauth.protected_metadata, {"email_only":True}),
    path(".well-known/oauth-authorization-server/conta/email", oauth.server_metadata, {"email_only":True}),
    path("conta/email/oauth/authorize/", oauth.Consent.as_view(email_only=True)),
    path("conta/email/oauth/register/", oauth.register, {"email_only":True}),
    path("conta/email/oauth/token/", oauth.token, {"email_only":True}),
    path("conta/a2a",a2a.endpoint),
    path(space_path+"configuracoes/",workspace_settings,name="workspace_settings"),
    path(space_path+"integracoes/",integrations,name="integrations"),
    path("conta/senha/", PasswordChangeView.as_view(template_name="password.html",success_url="/conta/"), name="password"),
    path("conta/instituicoes/<uuid:tenant>/conectores/email/",mail_views.connector,name="email_connector"),
    path("conta/instituicoes/<uuid:tenant>/meu-email/",mail_views.personal,name="my_email"),
    path("conta/instituicoes/<uuid:tenant>/conectores/github/",github.connector,name="github_connector"),
    path("conta/instituicoes/<uuid:tenant>/conectores/github/registro/",github.registered,name="github_registered"),
    path("conta/administracao/usuarios/",people,name="people"),
    path(space_path + "gestao/membros/",manage_member,name="manage_member"),
    path(space_path + "gestao/notificacoes/<uuid:agent>/", notification_config, name="notification_config"),
    path(space_path + "gestao/modelo/", configure_model, name="configure_model"),
    path(space_path, workspace, name="workspace"),
    path(space_path + "agentes/<uuid:agent>/", use_agent, name="use_agent"),
    path(space_path + "gestao/", manage_agents, name="manage_agents"),
    path(space_path + "gestao/<uuid:agent>/", manage_agents, name="edit_agent"),
    path("conta/administracao/", administration, name="administration"),
    path("conta/entrar/", PortalLogin.as_view()),
    path("conta/sair/", LogoutView.as_view()),
    path("conta/agentes/", active_agents, name="active_agents"),
    path("conta/", account, name="dashboard"),
]
