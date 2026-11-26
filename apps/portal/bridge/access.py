import re
from functools import wraps

from django.db import connection
from django.http import Http404

from space_views import space_context
from portal_identity import bind_account
from .workspaces import effective, visible
from .models import AccountProfile


ORIGIN = "https://hubia.psxt.io"
ICON_URL = ORIGIN + "/hub-ia.png"
MCP_ICONS = [
    {"src":ORIGIN + "/hub-ia-64.png", "mimeType":"image/png", "sizes":["64x64"]},
    {"src":ICON_URL, "mimeType":"image/png", "sizes":["512x512"]},
]
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
        icon = f'<{MCP_ICONS[0]["src"]}>; rel="icon"; type="image/png"; sizes="64x64"'
        response["Link"] = ", ".join(filter(None, [response.get("Link"), icon]))
        return response
    return decorated


def context(link, *, writing=False, multi_space=False):
    if not link.user.is_active:
        raise Http404
    with connection.cursor() as cursor:
        space = link.space
        spaces = None
        if link.protocol == "mcp" and not link._state.adding:
            principal, _ = bind_account(cursor, link.user)
            if not principal:
                raise Http404
            spaces = effective(link, visible(cursor, link.tenant))
            if not spaces or (space not in spaces and not multi_space):
                raise Http404
            if space not in spaces:
                space = next(iter(spaces))
        ctx = space_context(cursor,link.user,link.tenant,space)
        ctx["guide_available"] = AccountProfile.may_open_guide(link.user)
        if spaces is not None:
            ctx["spaces"] = spaces
        cursor.execute("SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s",[link.tenant])
        ctx["viewer"] = cursor.fetchone()[0]=="viewer"
        if writing and ctx["viewer"]:
            raise Http404
        return ctx
