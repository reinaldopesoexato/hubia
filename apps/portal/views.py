from uuid import UUID

from django.utils.translation import gettext as translate_text
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (LoginView, LogoutView, PasswordChangeView, PasswordResetConfirmView,
                                       PasswordResetView)
from django.views.generic import TemplateView
from django.db import DatabaseError, connection, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import path
from auth_limits import allow_auth_attempt
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.utils.text import slugify


from bridge import a2a, avihub, github, mail_views, mcp, oauth, platform_email
from bridge.access import MCP
from bridge.workspaces import manage as ai_workspaces
from bridge.portal import integrations
from bridge.models import AccountProfile, CatalogAlerts, LibraryAccess, Link, NotificationChannel
from bridge.catalog_alerts import cancel_pending
from dashboard import accessible_tenants, agent_directory, overview, tenant_workspaces
import registration
from languages import choose_language
from profile_photos import edit_profile, profile_photo
from account_admin import people, edit_person
from portal_identity import bind_account
from bridge.notifications import configure as notification_config
from space_views import configure_model, manage_agents, manage_member, use_agent, workspace


class PortalLogin(LoginView):
    authentication_form = registration.EmailLoginForm

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "google_enabled":registration.google_enabled()}

    template_name = "login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        source = request.META.get("HTTP_X_REAL_IP", request.META["REMOTE_ADDR"])
        if not allow_auth_attempt("portal-login", source, 20):
            return HttpResponse(translate_text("Muitas tentativas. Aguarde cinco minutos."), status=429,
                                headers={"Retry-After": "300"})
        return super().post(request, *args, **kwargs)


class PortalReset(PasswordResetView):
    form_class = platform_email.QueuedResetForm
    template_name = "password_reset.html"
    success_url = "/conta/recuperar-senha/solicitada/"

    def form_valid(self, form):
        if not platform_email.PlatformMailSettings.objects.filter(pk=1).exclude(encrypted_password=b"").exists():
            form.add_error(None, translate_text("A recuperação por e-mail está indisponível. Entre em contato com a administração do HUB IA."))
            return self.form_invalid(form)
        source = self.request.META.get("HTTP_X_REAL_IP", self.request.META["REMOTE_ADDR"])
        if not allow_auth_attempt("portal-reset-source", source, 10):
            return HttpResponse(translate_text("Muitas tentativas. Aguarde cinco minutos."), status=429, headers={"Retry-After": "300"})
        if not allow_auth_attempt("portal-reset-email", form.cleaned_data["email"].casefold(), 2):
            return redirect(self.success_url)
        return super().form_valid(form)


@never_cache
@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def account(request):
    from iaves_portal import dashboard_access, dashboard_library

    period = 30 if request.GET.get("dias") == "30" else 7
    with transaction.atomic(), connection.cursor() as cursor:
        principal, is_owner = bind_account(cursor, request.user)
        if principal is None:
            logout(request)
            return HttpResponse(translate_text("O acesso desta conta está desativado."), status=403)
        if request.method == "POST":
            if request.POST.get("action") != "defer_avihub":
                return HttpResponse(translate_text("Opção de configuração inválida."), status=400)
            AccountProfile.objects.update_or_create(user=request.user, defaults={"avihub_setup_deferred": True})
            return redirect("dashboard")
        context = overview(cursor, principal, period, request.user.pk)
        context["iaves"] = dashboard_access(cursor, context["spaces"])
        context["library_access"] = dashboard_library(cursor, context["spaces"])
        context["mcp_url"] = MCP
        context["avihub_setup"] = avihub.dashboard_setup(cursor, request.user, context["spaces"])
        context["setup_deferred"] = AccountProfile.objects.filter(user=request.user, avihub_setup_deferred=True).exists()
        context["guide_available"] = AccountProfile.may_open_guide(request.user)
    return render(request, "account.html", {**context, "is_owner": is_owner})


@never_cache
@login_required
@require_http_methods(["GET", "HEAD"])
def my_spaces(request):
    with transaction.atomic(), connection.cursor() as cursor:
        principal, owner = bind_account(cursor, request.user)
        if not principal:
            raise Http404
        spaces = [space for tenant in accessible_tenants(cursor) for space in tenant_workspaces(cursor, tenant)]
    return render(request, "my_spaces.html", {"spaces": spaces, "is_owner": owner,
                  "guide_available": AccountProfile.may_open_guide(request.user)})


@never_cache
@login_required
@require_http_methods(["GET"])
def active_agents(request, personal_guide=False):
    with transaction.atomic(), connection.cursor() as cursor:
        principal, is_owner = bind_account(cursor, request.user)
        if principal is None:
            logout(request)
            return HttpResponse(translate_text("O acesso desta conta está desativado."), status=403)
        guide_available = AccountProfile.may_open_guide(request.user)
        if personal_guide and not guide_available:
            raise Http404
        groups = agent_directory(cursor, include_empty=personal_guide)
    return render(request, "active_agents.html", {"groups":groups, "is_owner":is_owner, "guide":personal_guide, "guide_available":guide_available})


def apply_admin_form(cursor, form):
    action = form.get("action")
    actions = {"create_org", "rename_org", "activate", "suspend", "create_space",
               "rename_space", "grant_space", "revoke_space"}
    if action not in actions:
        raise ValueError(translate_text("Operação inválida."))
    try:
        tenant = None if action == "create_org" else UUID(form.get("tenant", ""))
        workspace = UUID(form.get("workspace", "")) if action in {"rename_space", "grant_space", "revoke_space"} else None
    except ValueError:
        raise ValueError(translate_text("Selecione uma instituição e um espaço válidos.")) from None
    name = form.get("name", "").strip()
    target = None
    username = form.get("username", "").strip()
    if action in {"grant_space", "revoke_space"} or (action == "create_org" and username):
        account_id = get_user_model().objects.filter(username=username, is_active=True).values_list("pk", flat=True).first()
        if account_id is not None:
            cursor.execute("SELECT hub_admin.portal_principal(%s)", [str(account_id)])
            target = cursor.fetchone()[0]
        if target is None:
            raise ValueError(translate_text("Usuário ativo não encontrado."))
    slug = slugify(name).replace("_", "-")[:80].rstrip("-")
    cursor.execute("SELECT hub_admin.manage_organization(%s,%s,%s,%s,%s,%s)",
                   [action, tenant, workspace, name, slug, target])


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def administration(request):
    from bridge import mail_access
    from bridge.iaves_metrics import report as iaves_report
    error = None
    groups = {}
    with transaction.atomic(), connection.cursor() as cursor:
        _, is_owner = bind_account(cursor, request.user)
        if not is_owner:
            return HttpResponse(translate_text("A administração exige uma conta de administrador geral."), status=403)
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
                    error = translate_text("Já existe um cadastro com esse identificador.")
                elif code == "22023":
                    error = exc.__cause__.diag.message_primary
                else:
                    raise
        cursor.execute("SELECT * FROM hub_admin.organization_directory()")
        for tenant, name, status, space, space_name in cursor.fetchall():
            org = groups.setdefault(tenant, {"id": tenant, "name": name, "status": status, "spaces": [], "mail_institution": tenant == mail_access.INSTITUTION})
            if space:
                org["spaces"].append({"id": space, "name": space_name})
        cursor.execute("SELECT tenant_id, workspace_id, workspace, deleted_at FROM hub_admin.workspace_inventory() WHERE deleted_at IS NOT NULL")
        for tenant, space, name, deleted_at in cursor.fetchall():
            groups[tenant].setdefault("trash", []).append({"id": space, "name": name, "deleted_at": deleted_at})
        iaves_metrics = iaves_report(7 if request.GET.get("dias_iaves") == "7" else 30)
    return render(request, "administration.html", {"organizations": groups.values(), "error": error, "is_owner": True,
                  "iaves_metrics":iaves_metrics,
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
                return HttpResponse(translate_text("Operação inválida."), status=400)
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
                (SELECT count(*) FROM hub_tenancy.workspace_agents WHERE tenant_id=%s AND workspace_id=%s AND active)""",
                [tenant,space,tenant,space])
            members, agents = cursor.fetchone()
            ctx["impact"] = [
                ("Membros ativos", members), ("Agentes disponíveis", agents),
                ("Conexões", Link.objects.filter(tenant=tenant,space=space).count()),
                ("Canais de notificação", NotificationChannel.objects.filter(tenant=tenant,space=space).count()),
            ]
            ctx["folders"] = LibraryAccess.objects.filter(library__connection_id=tenant,space=space).values_list("folders",flat=True).first() or []
            ctx["catalog_alerts"] = CatalogAlerts.objects.filter(tenant=tenant,space=space).exists()
    return render(request, "workspace_settings.html", ctx, status=400 if "error" in ctx else 200)


import avihub_notifications_portal
import avihub_reports_portal
from institution_agents import manage as institution_agents
from iaves_portal import memory as iaves_memory
from iaves_subscriptions_portal import subscriptions as iaves_subscriptions


space_path = "conta/espacos/<uuid:tenant>/<uuid:space>/"
urlpatterns = [
    path("conta/administracao/usuarios/<int:ident>/",edit_person,name="edit_person"),
    path("conta/administracao/usuarios/<int:ident>/excluir/",edit_person,{"deleting":True},name="delete_person"),
    path("conta/cadastrar/",registration.signup,name="signup"),
    path("conta/ativar/<str:token>/",registration.activate,name="activate_registration"),
    path("conta/google/",registration.google_start,name="google_start"),
    path("conta/google/retorno/",registration.google_callback,name="google_callback"),
    path("conta/google/vincular/",registration.google_link,name="google_link"),
    path("conta/idioma/", choose_language, name="choose_language"),
    path("conta/perfil/", edit_profile, name="edit_profile"),
    path("conta/perfil/foto/", profile_photo, name="profile_photo"),
    path("conta/espacos/", my_spaces, name="my_spaces"),
    path("conta/guia/", active_agents, {"personal_guide":True}, name="personal_guide"),
    path("conta/administracao/instituicoes/<uuid:tenant>/agentes/", institution_agents, name="institution_agents"),
    path("conta/administracao/instituicoes/<uuid:tenant>/agentes/<uuid:agent>/", institution_agents, name="institution_agent_edit"),
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
    path("conta/administracao/email/", platform_email.configure, name="platform_email"),
    path("conta/recuperar-senha/", PortalReset.as_view(), name="password_reset"),
    path("conta/recuperar-senha/solicitada/", TemplateView.as_view(template_name="password_reset_done.html"), name="password_reset_done"),
    path("conta/recuperar-senha/concluida/", TemplateView.as_view(template_name="password_reset_complete.html"), name="password_reset_complete"),
    path("conta/recuperar-senha/<uidb64>/<token>/", PasswordResetConfirmView.as_view(
        template_name="password_reset_confirm.html"), name="password_reset_confirm"),
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
    path(space_path + "agentes/<uuid:agent>/notificacoes/", avihub_notifications_portal.notifications,
         name="avihub_notifications"),
    path(space_path + "agentes/<uuid:agent>/relatorios/", avihub_reports_portal.reports, name="avihub_reports"),
    path(space_path + "agentes/<uuid:agent>/relatorios/<uuid:report_id>/arquivo/", avihub_reports_portal.download,
         name="avihub_report_download"),
    path(space_path + "agentes/<uuid:agent>/memoria/", iaves_memory, name="iaves_memory"),
    path(space_path + "agentes/<uuid:agent>/avisos/", iaves_subscriptions, name="iaves_subscriptions"),
    path(space_path + "gestao/", manage_agents, name="manage_agents"),
    path(space_path + "gestao/<uuid:agent>/", manage_agents, name="edit_agent"),
    path("conta/administracao/", administration, name="administration"),
    path("conta/entrar/", PortalLogin.as_view(),name="login"),
    path("conta/sair/", LogoutView.as_view()),
    path("conta/agentes/", active_agents, name="active_agents"),
    path("conta/", account, name="dashboard"),
]
