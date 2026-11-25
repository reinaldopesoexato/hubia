import hashlib
import re
import json
import secrets
from base64 import b64decode
from binascii import Error as Base64Error
from urllib.parse import urlsplit, parse_qs, unquote_plus
from django.contrib.auth.views import redirect_to_login
from django.db import connection, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from dashboard import accessible_tenants
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from oauth2_provider.models import get_access_token_model, get_application_model
from portal_identity import bind_account
from oauth2_provider.views import AuthorizationView, TokenView

from .access import ORIGIN, MCP, META, MAIL_MCP, MAIL_ISSUER, MAIL_SCOPES, email_title, context, with_icon_link
from .models import Link, Mailbox, MailProvider, WorkspaceSelection
from .workspaces import selection


@require_GET
@with_icon_link
def protected_metadata(request, email_only=False):
    if email_only:
        return JsonResponse({"resource":MAIL_MCP, "resource_name":"Meu Email", "authorization_servers":[MAIL_ISSUER],
                             "scopes_supported":MAIL_SCOPES, "bearer_methods_supported":["header"]})
    return JsonResponse({"resource":MCP,"resource_name":"HUB IA","authorization_servers":[ORIGIN],
        "scopes_supported":["hub.read","hub.reply","hub.notify","hub.mail.send","offline_access"],"bearer_methods_supported":["header"]})


@require_GET
@with_icon_link
def server_metadata(request, email_only=False):
    issuer = MAIL_ISSUER if email_only else ORIGIN
    base = MAIL_ISSUER if email_only else ORIGIN + "/conta"
    return JsonResponse({"issuer":issuer,"authorization_endpoint":base+"/oauth/authorize/",
        "registration_endpoint":base+"/oauth/register/",
        "token_endpoint":base+"/oauth/token/","response_types_supported":["code"],
        "grant_types_supported":["authorization_code","refresh_token"],
        "scopes_supported":MAIL_SCOPES if email_only else ["hub.read","hub.reply","hub.notify","hub.mail.send","offline_access"],
        "token_endpoint_auth_methods_supported":["client_secret_basic","client_secret_post","none"],
        "code_challenge_methods_supported":["S256"]})


DYNAMIC_CLIENTS = {
    "ChatGPT · HUB IA": re.compile(r"https://chatgpt\.com/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]{1,160})"),
    "Gemini · HUB IA": re.compile(r"https://oauth-redirect(?:-sandbox|-test)?\.googleusercontent\.com/[ar]/[A-Za-z0-9_-][A-Za-z0-9_.~-]{0,511}"),
}


MAIL_CLIENTS = {name.replace("HUB IA", "Meu Email") for name in DYNAMIC_CLIENTS}


@csrf_exempt
def register(request, email_only=False):
    if request.method!="POST" or request.content_type!="application/json" or len(request.body)>8192:
        return JsonResponse({"error":"invalid_client_metadata"},status=400)
    try:
        data=json.loads(request.body)
        uris=data["redirect_uris"]
        if not isinstance(uris, list) or not 1 <= len(uris) <= 8 or not all(isinstance(uri, str) for uri in uris):
            raise ValueError
        name = next((name for name, pattern in DYNAMIC_CLIENTS.items() if all(pattern.fullmatch(uri) for uri in uris)), None)
        if not name or (name == "ChatGPT · HUB IA" and len(uris) != 1):
            raise ValueError
        method=data.get("token_endpoint_auth_method","client_secret_basic")
        if method not in {"client_secret_basic","client_secret_post","none"}:
            raise ValueError
    except (TypeError, ValueError, KeyError):
        return JsonResponse({"error":"invalid_client_metadata"},status=400)
    names = set(DYNAMIC_CLIENTS) | set(MAIL_CLIENTS)
    if email_only:
        name = name.replace("HUB IA", "Meu Email")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(164201, 9)")
        app_cls=get_application_model()
        if app_cls.objects.filter(user__isnull=True,name__in=names).count()>=1000:
            return JsonResponse({"error":"temporarily_unavailable"},status=429)
        key=hashlib.sha256(("dcr:"+str(request.META.get("HTTP_X_REAL_IP",request.META.get("REMOTE_ADDR")))).encode()).hexdigest()
        cursor.execute("""INSERT INTO login_limits(source) VALUES(%s) ON CONFLICT(source) DO UPDATE
            SET started_at=CASE WHEN login_limits.started_at<now()-interval '5 minutes' THEN now() ELSE login_limits.started_at END,
                attempts=CASE WHEN login_limits.started_at<now()-interval '5 minutes' THEN 1 ELSE login_limits.attempts+1 END
            WHERE login_limits.attempts<15 OR login_limits.started_at<now()-interval '5 minutes' RETURNING attempts""",[key])
        if not cursor.fetchone():
            return JsonResponse({"error":"temporarily_unavailable"},status=429)
        secret=secrets.token_urlsafe(48)
        app=app_cls.objects.create(name=name,client_type="public" if method=="none" else "confidential",
            authorization_grant_type="authorization-code",client_secret=secret,redirect_uris=" ".join(uris))
    info={"client_id":app.client_id,"redirect_uris":uris,"token_endpoint_auth_method":method,
          "grant_types":["authorization_code","refresh_token"],"response_types":["code"]}
    if method!="none":
        info.update(client_secret=secret,client_secret_expires_at=0)
    return JsonResponse(info,status=201,headers={"Cache-Control":"no-store"})


def default_link(user, client_id):
    app=get_application_model().objects.filter(client_id=client_id,user__isnull=True,name="ChatGPT · HUB IA").first()
    if not app:
        raise Http404
    with connection.cursor() as cursor:
        principal,_=bind_account(cursor,user)
        if not principal:
            raise Http404
        cursor.execute("SELECT tenant_id FROM hub_tenancy.memberships WHERE role='personal_owner'")
        row=cursor.fetchone()
        if not row:
            raise Http404
        tenant=row[0]
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)",[str(tenant)])
        cursor.execute("SELECT id FROM hub_tenancy.workspaces WHERE tenant_id=%s AND slug='principal'",[tenant])
        row=cursor.fetchone()
        if not row:
            raise Http404
    return Link(user=user,tenant=tenant,space=row[0],name="Dot · ChatGPT",protocol="mcp",client_id=client_id,writable=True)


def pick_workspace(request, client_id, selected, *, email_only=False):
    with connection.cursor() as cursor:
        principal, _ = bind_account(cursor, request.user)
        if not principal:
            raise Http404
        choices = {}
        groups = []
        mail_tenants = set(Mailbox.objects.filter(user=request.user, provider__enabled=True).values_list("provider_id", flat=True)) if email_only else None
        for tenant in accessible_tenants(cursor):
            if email_only and tenant not in mail_tenants:
                continue
            cursor.execute("""SELECT w.id, w.name, i.name FROM hub_tenancy.workspaces w
                              LEFT JOIN hub_tenancy.institutions i ON i.tenant_id=w.tenant_id
                              WHERE w.tenant_id=%s ORDER BY (w.slug='principal') DESC, lower(w.name),w.id""", [tenant])
            rows = cursor.fetchall()
            if not rows:
                continue
            organization = rows[0][2] or "Espaço pessoal"
            all_key = f"{tenant}:all"
            choices[all_key] = (tenant, rows[0][0], organization)
            options = [(all_key, "Todos os meus espaços atuais e futuros")]
            if email_only:
                groups.append((organization, [(all_key, email_title(organization))]))
                continue
            for space, workspace, _ in rows:
                key = f"{tenant}:{space}"
                choices[key] = (tenant, space, organization)
                options.append((key, workspace))
            groups.append((organization, options))
    if not selected and request.method == "GET":
        params = [(key, value) for key, values in request.GET.lists() if key != "hub_workspace" for value in values]
        return render(request, "gemini_workspace.html", {"params":params, "groups":groups, "email_only":email_only})
    if selected not in choices:
        raise Http404
    tenant, space, organization = choices[selected]
    return Link(user=request.user, tenant=tenant, space=space, name=email_title(organization)[:100] if email_only else "Gemini", protocol="mcp",
                client_id=client_id, writable=not email_only, email_only=email_only)


@method_decorator(never_cache, name="dispatch")
class Consent(AuthorizationView):
    template_name = "bridge_consent.html"
    email_only = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        data = request.POST if request.method=="POST" else request.GET
        client_id = data.get("client_id", "")
        selected = data.get("hub_workspace", "")
        resource = MAIL_MCP if self.email_only else MCP
        if (data.get("resource",resource)!=resource or len(data.getlist("resource"))>1 or
                data.get("code_challenge_method")!="S256" or
                not re.fullmatch(r"[A-Za-z0-9_-]{43}",data.get("code_challenge",""))):
            return JsonResponse({"error":"invalid_request"},status=400)
        with transaction.atomic():
            links = Link.objects.filter(client_id=client_id, user=request.user, protocol="mcp")
            link = links.first()
            if link is None:
                names = MAIL_CLIENTS if self.email_only else {"Gemini · HUB IA"}
                apps = get_application_model().objects.filter(client_id=client_id, user__isnull=True, name__in=names)
                app = (apps.select_for_update() if request.method == "POST" else apps).first()
                if app:
                    link = links.first() or pick_workspace(request, client_id, selected, email_only=self.email_only)
                    if not isinstance(link, Link):
                        return link
                elif self.email_only:
                    raise Http404
                else:
                    link = default_link(request.user, client_id)
            if link.email_only != self.email_only:
                return JsonResponse({"error":"invalid_target"}, status=400)
            picked_all = selected == f"{link.tenant}:all"
            already_all = not link._state.adding and selection(link) is None
            if picked_all and not (link._state.adding or already_all):
                return JsonResponse({"error":"invalid_request"}, status=400)
            if selected and not picked_all and selected != f"{link.tenant}:{link.space}":
                return JsonResponse({"error":"invalid_request"}, status=400)
            allowed = {"hub.read","hub.mail.send","offline_access"} | ({"hub.reply","hub.notify"} if link.writable else set())
            if not set(data.get("scope","").split())<=allowed:
                return JsonResponse({"error":"invalid_scope"},status=400)
            self.space_info = context(link, multi_space=True)
            self.space_info["email_only"] = self.email_only
            self.space_info["hub_workspace"] = selected
            self.space_info["all_spaces"] = picked_all or already_all
            self.space_info["offer_all_spaces"] = not self.email_only and not selected and not already_all
            # Serialize consent with changes to the user's mailbox and its permissions.
            provider = MailProvider.objects.filter(pk=link.tenant, enabled=True)
            if request.method == "POST":
                provider = provider.select_for_update()
            profile = provider.first()
            if request.method == "POST" and not link._state.adding:
                link = Link.objects.select_for_update().filter(pk=link.pk).first()
                if link is None:
                    raise Http404
            scopes = set(data.get("scope", "hub.read").split())
            offer_read = link.offer_mail_access and "hub.read" in scopes
            offer_send = "hub.mail.send" in scopes and not self.space_info["viewer"]
            box = None
            if profile and (offer_read or offer_send):
                box = Mailbox.objects.only("id", "address").filter(provider=profile, user=request.user).first()
            self.space_info["mailbox_offer"] = box if offer_read else None

            # Bind the send choice to the displayed mailbox and authorization attempt.
            # Older forms and clients that request only read access leave this grant untouched.
            send_box = box if offer_send else None
            send_enabled = bool(send_box and not link._state.adding and send_box.send_links.filter(pk=link.pk).exists())
            session_key = "mcp_mail_send:" + client_id
            binding = [str(send_box.pk) if send_box else "", data["code_challenge"], send_enabled]
            send_choice = None
            if request.method == "GET" and send_box:
                nonce = secrets.token_urlsafe(24)
                request.session[session_key] = [nonce, *binding]
                self.space_info.update(send_box=send_box, send_nonce=nonce, send_enabled=send_enabled)
            if request.method == "POST" and ("mail_send_offer" in data or "allow_mail_send" in data):
                if not send_box or not data.get("mail_send_offer") or request.session.get(session_key) != [data["mail_send_offer"], *binding]:
                    if data.get("allow"):
                        return JsonResponse({"error":"invalid_request", "error_description":
                                             "Recarregue a autorização para confirmar a permissão de envio."}, status=400)
                else:
                    del request.session[session_key]
                    send_choice = data.get("allow_mail_send") == "on"
            response = super().dispatch(request,*args,**kwargs)
            if request.method == "POST" and request.POST.get("allow") and response.status_code == 302:
                result = parse_qs(urlsplit(response.get("Location", "")).query)
                if "code" in result and "error" not in result:
                    if link._state.adding:
                        link, _ = Link.objects.select_for_update().get_or_create(
                            client_id=link.client_id, user=request.user, defaults={"tenant":link.tenant,
                            "space":link.space, "name":link.name, "protocol":"mcp", "writable":link.writable, "email_only":self.email_only})
                    if picked_all or (self.space_info["offer_all_spaces"] and request.POST.get("allow_all_spaces") == "on"):
                        WorkspaceSelection.objects.update_or_create(link=link, defaults={"spaces":None})
                    if send_choice is not None and send_choice != send_enabled:
                        if send_choice:
                            send_box.send_links.add(link)
                        else:
                            send_box.send_links.remove(link)
                    if offer_read and box and link.offer_mail_access and request.POST.get("mailbox_offer") == str(box.pk):
                        if request.POST.get("allow_mail") == "on":
                            box.links.add(link)
                        link.offer_mail_access = False
                        link.save(update_fields=["offer_mail_access"])
            return response

    def get_initial(self):
        initial = super().get_initial()
        # Clients may omit the optional resource on GET. Preserve the HUB
        # audience in the hidden field when the consent form is submitted.
        if not initial.get("resource"):
            initial["resource"] = MAIL_MCP if self.email_only else MCP
        return initial

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs),**self.space_info}


@csrf_exempt
def token(request, email_only=False):
    resource = MAIL_MCP if email_only else MCP
    if request.method!="POST" or request.POST.get("resource",resource)!=resource or len(request.POST.getlist("resource"))>1:
        return JsonResponse({"error":"invalid_target"},status=400)
    client_id = request.POST.get("client_id", "")
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            basic_id = unquote_plus(b64decode(credentials, validate=True).decode().split(":", 1)[0])
        except (Base64Error, UnicodeError):
            return JsonResponse({"error":"invalid_client"}, status=401)
        if client_id and client_id != basic_id:
            return JsonResponse({"error":"invalid_client"}, status=401)
        client_id = basic_id
    is_mail_client = (get_application_model().objects.filter(client_id=client_id, user__isnull=True, name__in=MAIL_CLIENTS).exists()
                      or Link.objects.filter(client_id=client_id, email_only=True).exists())
    if email_only != is_mail_client:
        return JsonResponse({"error":"invalid_target"}, status=400)
    return TokenView.as_view()(request)


def unauthorized(email_only=False):
    meta = META + "/email" if email_only else META
    return JsonResponse({"error":"invalid_token"},status=401,headers={
        "WWW-Authenticate":f'Bearer resource_metadata="{meta}", scope="hub.read"',"Cache-Control":"no-store"})


def bearer(request):
    scheme,_,value = request.headers.get("Authorization","").partition(" ")
    return value if scheme.lower()=="bearer" and 0<len(value)<=512 else ""


def authenticate_mcp(request, email_only=False):
    value = bearer(request)
    if not value:
        return None
    grant = get_access_token_model().objects.select_related("application","user").filter(
        token_checksum=hashlib.sha256(value.encode()).hexdigest()).first()
    if not grant or not grant.is_valid(["hub.read"]) or not grant.user or not grant.user.is_active:
        return None
    link = Link.objects.select_related("user").filter(protocol="mcp",email_only=email_only,client_id=grant.application.client_id,
                                                      user=grant.user).first()
    if not link:
        return None
    ctx = context(link, multi_space=True)
    can_reply = link.writable and not ctx["viewer"] and grant.allow_scopes(["hub.reply"])
    ctx["token_id"] = grant.pk
    ctx["can_notify"] = link.writable and not ctx["viewer"] and grant.allow_scopes(["hub.notify"])
    ctx["can_send_mail"] = not ctx["viewer"] and grant.allow_scopes(["hub.mail.send"])
    return link,ctx,can_reply
