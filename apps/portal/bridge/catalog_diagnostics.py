"""Compare the last MCP listing with permissions and sources available locally."""

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from oauth2_provider.models import get_access_token_model

from . import avihub, library, mail_access, mcp
from .models import NoteLibrary, PESO_EXATO_TENANT_ID
from .oauth import apply_mcp_scopes


def active_scopes(user, links):
    clients = [link.client_id for link in links if link.client_id]
    grants = get_access_token_model().objects.filter(
        user=user, application__client_id__in=clients, expires__gt=timezone.now(),
    ).order_by("-created", "-pk").values_list("application__client_id", "scope")
    scopes = {}
    for client, raw in grants:
        granted = set(raw.split())
        if "hub.read" in granted:
            scopes.setdefault(client, granted)
    return scopes


def source_actions(cursor, link, space_tenants, names, item):
    if "consultar_iaves" in names:
        source = NoteLibrary.objects.select_related("connection").filter(
            connection_id=PESO_EXATO_TENANT_ID, repository_id=library.IAVES_REPOSITORY_ID,
        ).first()
        if not source or not library.snapshot_status(source):
            yield _("A base local do IAves aguarda sincronização. Peça ao administrador para conferir a cópia da base."), None
    if "listar_contas_avihub" in names or "buscar_notas" not in names:
        spaces_by_tenant = {}
        for space, tenant in space_tenants.items():
            spaces_by_tenant.setdefault(tenant, []).append(space)
        for tenant, spaces in spaces_by_tenant.items():
            mcp.activate_tenant(cursor, tenant)
            cursor.execute("""SELECT DISTINCT ON (kind) kind, workspace_id, id FROM hub_tenancy.workspace_agents
                WHERE tenant_id=%s AND workspace_id=ANY(%s::uuid[]) AND active AND kind=ANY(%s)
                ORDER BY kind, workspace_id, id""", [tenant, spaces, ["avihub", "library"]])
            for kind, space, agent in cursor.fetchall():
                if kind == "avihub" and "listar_contas_avihub" in names:
                    if not avihub.personal(tenant, link.user).filter(ready=True).exists():
                        yield _("Vincule ou renove sua conta AviHUB para consultar seus dados."), reverse(
                            "use_agent", args=[tenant, space, agent])
                if kind == "library" and "buscar_notas" not in names:
                    yield _("Peça ao administrador para liberar as pastas da biblioteca neste espaço."), reverse(
                        "integrations", args=[tenant, space])
    if item["mail_space"]:
        if not item["box"]:
            yield _("Vincule sua caixa institucional para usar as ferramentas de e-mail."), reverse(
                "my_email", args=[mail_access.tenant_for(link)])
        elif "listar_emails_novos" not in names:
            yield _("Marque a permissão de leitura de e-mail nesta conexão e salve."), None


def diagnose(cursor, link, item, chosen, spaces, space_tenants, roles, scopes, guide_available):
    listing = getattr(link, "catalog_listing", None)
    previous = listing.tools if listing is not None else None
    current = []
    actions = []
    if chosen == []:
        actions.append((_("Selecione espaços e salve para retomar o acesso desta conexão."), None))
    elif not space_tenants and item["choices"]:
        actions.append((_("A conexão perdeu acesso aos espaços escolhidos. Selecione espaços disponíveis e salve."), None))
    elif not space_tenants:
        actions.append((_("Solicite acesso a um espaço para voltar a usar esta conexão."), None))
    elif not scopes:
        actions.append((_("Reconecte o HUB IA no seu cliente de IA para renovar a autorização OAuth."), None))
    elif link.email_only and not item["mail_space"]:
        actions.append((_("Inclua um espaço da instituição do e-mail e salve."), None))
    else:
        ctx = {"spaces": {space: spaces[space] for space in space_tenants}, "space_tenants": space_tenants,
               "tenant_roles": roles, "guide_available": guide_available}
        can_reply = apply_mcp_scopes(link, ctx, scopes)
        current = [spec["name"] for spec in mcp.available_tools(link, ctx, can_reply)]
        names = set(current)
        if chosen is not None and set(chosen) - {str(space) for space in space_tenants}:
            actions.append((_("Parte dos espaços escolhidos perdeu acesso. Revise os espaços desta conexão."), None))
        if not link.email_only and link.space not in space_tenants:
            actions.append((_("Inclua o espaço de origem para disponibilizar as ferramentas de tarefas."), None))
        if "consultar_iaves" in names and "memorizar_iaves" not in names:
            actions.append((_("Autorize novamente a conexão com o escopo hub.iaves.memory para usar a memória do IAves."), None))
        if link.writable and roles.get(link.tenant) not in (None, "viewer") and "list_tasks" in names and "hub.reply" not in scopes:
            actions.append((_("Autorize novamente a conexão com o escopo hub.reply para responder tarefas."), None))
        if link.writable and "listar_destinos" in names and "hub.notify" not in scopes:
            actions.append((_("Autorize novamente a conexão com o escopo hub.notify para enviar avisos."), None))
        if item["send_mail"] and item["can_enable_send"] and "hub.mail.send" not in scopes:
            actions.append((_("Autorize novamente a conexão com o escopo hub.mail.send para enviar e-mails."), None))
        actions.extend(source_actions(cursor, link, space_tenants, names, item))
        if not current and not actions:
            actions.append((_("Revise as permissões de leitura e envio de e-mail desta conexão."), None))
        if previous is None or names - set(previous):
            actions.append((_("Atualize o catálogo do HUB IA no seu cliente de IA para consultar as ferramentas disponíveis."), None))
        if previous and set(previous) - names:
            actions.append((_("Confira os espaços, os agentes ativos e as permissões da conexão para recuperar as ferramentas ausentes."), None))
    return {
        "listed_at": listing.listed_at if listing is not None else link.previous_listing_at,
        "recorded": listing is not None,
        "previous": previous or [],
        "current": current,
        "removed": [tool for tool in previous or [] if tool not in current],
        "actions": actions,
    }
