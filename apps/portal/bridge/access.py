import re
from functools import wraps

from django.db import connection
from django.http import Http404

from space_views import space_context
from portal_identity import bind_account
from .workspaces import effective, effective_spaces, visible
from .models import AccountProfile


ORIGIN = "https://hubia.psxt.io"
HUB_NAME = "HUB IA"
HUB_SITE = ORIGIN + "/"
ICON_URL = ORIGIN + "/hub-ia.png"
MCP_ICONS = [
    {"src":ORIGIN + "/hub-ia-64.png", "mimeType":"image/png", "sizes":["64x64"]},
    {"src":ICON_URL, "mimeType":"image/png", "sizes":["512x512"]},
]
MCP_SERVER_INFO = {
    "name": "hub-ia",
    "title": HUB_NAME,
    "version": "1.0.0",
    "description": "Acesse agentes, serviços e dados autorizados no HUB IA da Peso Exato.",
    "websiteUrl": HUB_SITE,
    "icons": MCP_ICONS,
}
MCP = ORIGIN + "/conta/mcp"
META = ORIGIN + "/.well-known/oauth-protected-resource/conta/mcp"


MAIL_MCP = MCP + "/email"
MAIL_ISSUER = ORIGIN + "/conta/email"
MAIL_SCOPES = ["hub.read", "hub.mail.send", "offline_access"]


def email_title(organization):
    name = re.sub(r"\s+ltda\.?$", "", organization.strip(), flags=re.IGNORECASE)
    return f"Meu Email {name}"


def with_icon_link(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        response = view(*args, **kwargs)
        icons = [f'<{icon["src"]}>; rel="icon"; type="{icon["mimeType"]}"; sizes="{icon["sizes"][0]}"'
                 for icon in MCP_ICONS]
        response["Link"] = ", ".join(filter(None, [response.get("Link"), *icons]))
        return response
    return decorated


def context(link, *, writing=False, multi_space=False):
    if not link.user.is_active:
        raise Http404
    with connection.cursor() as cursor:
        tenant = link.tenant
        space = link.space
        groups = None
        if link.protocol == "mcp" and (not link._state.adding or link.all_tenants):
            principal, _ = bind_account(cursor, link.user)
            if not principal:
                raise Http404
            groups = effective_spaces(cursor, link) if multi_space else {tenant: effective(link, visible(cursor, tenant))}
            if space not in groups.get(tenant, {}):
                if not groups or not multi_space:
                    raise Http404
                tenant = next(iter(groups))
                space = next(iter(groups[tenant]))
        ctx = space_context(cursor, link.user, tenant, space)
        ctx["guide_available"] = AccountProfile.may_open_guide(link.user)
        cursor.execute("""SELECT tenant_id, role FROM hub_tenancy.memberships
                          WHERE tenant_id=ANY(%s::uuid[]) AND active
                            AND user_id=nullif(current_setting('hub.principal_id', true),'')::uuid""",
                       [list(groups) if groups is not None else [tenant]])
        roles = dict(cursor.fetchall())
        if tenant not in roles:
            raise Http404
        ctx["tenant_roles"] = roles
        if groups is not None:
            ctx["spaces"] = {key: name for spaces in groups.values() for key, name in spaces.items()}
            ctx["space_tenants"] = {key: group_tenant for group_tenant, spaces in groups.items() for key in spaces}
        ctx["viewer"] = roles[tenant] == "viewer"
        if writing and ctx["viewer"]:
            raise Http404
        return ctx
