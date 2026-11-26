import hashlib
import re

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from model_provider import cipher
from portal_identity import bind_account
from . import mail_access
from .access import MAIL_MCP, email_title
from .github import institution
from .mail_protocol import inbox, IMAP_HOST, SMTP_HOST
from .signature import signature_parts
from .models import Link, Mailbox, MailProvider

DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def connector(request, tenant):
    with transaction.atomic(), connection.cursor() as cursor:
        if tenant != mail_access.INSTITUTION:
            raise Http404
        ctx = institution(cursor, request.user, tenant)
        provider = MailProvider.objects.select_for_update().filter(pk=tenant).first()
        if request.method == "POST":
            domain = request.POST.get("domain", "").strip().lower()
            if not DOMAIN.fullmatch(domain):
                return HttpResponseBadRequest("Informe um domínio corporativo válido.")
            if provider and provider.domain != domain and provider.mailbox_set.exists():
                return HttpResponseBadRequest("Desconecte as caixas vinculadas antes de alterar o domínio.")
            MailProvider.objects.update_or_create(tenant=tenant, defaults={
                "domain":domain, "enabled":request.POST.get("enabled") == "on"})
            return redirect("email_connector", tenant)
        ctx.update(provider=provider, imap=IMAP_HOST, smtp=SMTP_HOST)
        return render(request, "email_connector.html", ctx)


@never_cache
@login_required
@sensitive_post_parameters("password")
@require_http_methods(["GET", "POST"])
def personal(request, tenant):
    with transaction.atomic(), connection.cursor() as cursor:
        principal, _ = bind_account(cursor, request.user)
        cursor.execute("SELECT set_config('hub.tenant_id',%s,true)", [str(tenant)])
        cursor.execute("""SELECT i.name FROM hub_tenancy.institutions i
                          JOIN hub_tenancy.memberships m USING (tenant_id)
                          JOIN hub_tenancy.tenants t ON t.id=i.tenant_id
                          WHERE i.tenant_id=%s AND m.user_id=%s AND m.active AND t.status='ACTIVE'""",
                       [tenant, principal])
        org = cursor.fetchone()
        if not org or not mail_access.allowed(cursor, tenant):
            raise Http404
        providers = MailProvider.objects.filter(pk=tenant)
        provider = (providers.select_for_update() if request.method=="POST" else providers).first()
        boxes = Mailbox.objects.filter(provider=provider, user=request.user)
        box = (boxes.select_for_update() if request.method == "POST" else boxes).first()
        error = ""
        source = box.signature_html if box else ""
        revision = hashlib.sha256(source.encode()).hexdigest()
        saved = request.session.pop("saved_mail_signature", None) == str(tenant)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "disconnect" and box:
                box.delete()
            elif not provider or not provider.enabled:
                return HttpResponseBadRequest("O conector está desativado.")
            elif action in {"signature", "preview_signature"} and box:
                source = request.POST.get("signature_html")
                if source is None or request.POST.get("mailbox") != str(box.pk):
                    return HttpResponseBadRequest("A caixa foi alterada. Reabra Meu e-mail.")
                try:
                    html, _ = signature_parts(source)
                except ValueError as exc:
                    error = str(exc)
                    if action == "preview_signature":
                        return JsonResponse({"error":error}, status=400)
                else:
                    if action == "preview_signature":
                        return JsonResponse({"html":html})
                    if request.POST.get("revision") != revision:
                        return HttpResponseBadRequest("A assinatura mudou. Reabra Meu e-mail antes de editar novamente.")
                    box.signature_html = html
                    box.save(update_fields=["signature_html"])
                    request.session["saved_mail_signature"] = str(tenant)
            elif action == "connect":
                address = request.POST.get("address", "").strip()
                password = request.POST.get("password", "")
                localpart, sep, domain = address.rpartition("@")
                if (not sep or domain.lower()!=provider.domain or len(address)>254
                        or not re.fullmatch(r"[A-Za-z0-9_.+%-]{1,64}", localpart)
                        or not password or len(password)>1024 or "\0" in password or "\n" in password or "\r" in password):
                    error = "Informe seu endereço corporativo completo e a senha da caixa."
                else:
                    address = localpart + "@" + provider.domain
                    try:
                        with inbox(address, password):
                            pass
                    except ValueError as exc:
                        error = str(exc)
                    else:
                        previous_signature = ""
                        if box:
                            if box.address == address:
                                previous_signature = box.signature_html
                            box.delete()
                        Mailbox.objects.create(provider=provider, user=request.user, address=address,
                                               password_cipher=cipher().encrypt(password.encode()), signature_html=previous_signature)
            elif action == "grant" and box:
                links = list(Link.objects.filter(user=request.user, tenant=tenant, protocol="mcp"))
                selected = set(request.POST.getlist("links"))
                if selected - {str(link.pk) for link in links}:
                    return HttpResponseBadRequest("Selecione apenas suas conexões desta instituição.")
                box.links.set([link for link in links if str(link.pk) in selected])
                Link.objects.filter(pk__in=[link.pk for link in links], offer_mail_access=True).update(offer_mail_access=False)
            else:
                return HttpResponseBadRequest("Ação indisponível.")
            if not error:
                return redirect("my_email", tenant)
        return render(request, "my_email.html", {"tenant":tenant, "organization":org[0], "provider":provider,
                      "box":box, "error":error, "signature_source":source,
                      "mail_mcp":MAIL_MCP, "mail_title":email_title(org[0]), "signature_revision":revision, "signature_saved":saved},
                      status=400 if error else 200)
