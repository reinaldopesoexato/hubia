from uuid import UUID

from django.utils.translation import gettext as translate_text
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from portal_identity import bind_account
from . import mail_access
from .models import CatalogObservation, Link, MailProvider, Mailbox, WorkspaceSelection


def visible(cursor, tenant):
    cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
    cursor.execute("""SELECT w.id, w.name, i.name FROM hub_tenancy.workspaces w
                      LEFT JOIN hub_tenancy.institutions i ON i.tenant_id=w.tenant_id
                      WHERE w.tenant_id=%s ORDER BY lower(w.name), w.id""", [tenant])
    return [(space, name, org or translate_text("Espaço pessoal")) for space, name, org in cursor.fetchall()]


def mail_write_access(cursor, tenant):
    cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
    cursor.execute("""SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s AND active
                      AND user_id=current_setting('hub.principal_id')::uuid""", [tenant])
    row = cursor.fetchone()
    return bool(row and row[0] != "viewer")


def selection(link):
    try:
        return link.workspaceselection.spaces
    except WorkspaceSelection.DoesNotExist:
        return None


def effective(link, rows):
    ids = selection(link)
    allowed = set(ids) if ids is not None else None
    return {space:name for space,name,_ in rows if allowed is None or str(space) in allowed}


def state_key(spaces):
    return "all" if spaces is None else ",".join(sorted(spaces))


def save_permissions(request, cursor, links):
    form = request.POST
    # Keep forms opened before deployment usable.
    legacy = "mode" in form
    try:
        keys = [UUID(value) for value in form.getlist("link")]
    except ValueError:
        raise Http404
    if not keys or len(keys) != len(set(keys)) or (legacy and len(keys) != 1):
        return HttpResponseBadRequest(translate_text("Seleção de conexões inválida."))
    requested = links.filter(pk__in=keys)
    if any(key.startswith("mailbox-") for key in form):
        # The mailbox connection and OAuth consent views also lock providers first.
        list(MailProvider.objects.filter(pk__in=requested.values("tenant")).order_by("pk").select_for_update())
    owned = list(requested.order_by("pk").select_for_update())
    if len(owned) != len(keys):
        raise Http404

    groups = {}
    changes = []
    mail_changes = []
    for link in owned:
        suffix = "" if legacy else f"-{link.pk}"
        mode = form.get("mode" + suffix)
        if mode not in ("all", "selected", "paused"):
            return HttpResponseBadRequest(translate_text("Escolha o acesso de cada conexão."))
        selected = set(form.getlist("spaces" + suffix))
        desired = None if mode == "all" else sorted(selected) if mode == "selected" else []
        initial = form.get("initial" + suffix)
        if not legacy and initial is None:
            return HttpResponseBadRequest(translate_text("Recarregue as permissões antes de salvar."))
        mail_id = form.get("mailbox" + suffix) if not legacy else None
        mail_edits = []
        if mail_id:
            if not mail_access.allowed(cursor, link.tenant):
                raise Http404
            for field, relation in (("read", "links"), ("send", "send_links")):
                original = form.get("initial-" + field + suffix)
                if original not in ("0", "1"):
                    return HttpResponseBadRequest(translate_text("Recarregue as permissões de e-mail."))
                checked = form.get(field + suffix) == "on"
                if checked != (original == "1"):
                    mail_edits.append((relation, checked))
        if not mail_edits and not legacy and initial == state_key(desired):
            continue
        if link.tenant not in groups:
            groups[link.tenant] = visible(cursor, link.tenant)
        rows = groups[link.tenant]
        if not rows:
            raise Http404
        if mail_edits:
            box = Mailbox.objects.filter(provider_id=link.tenant, user=request.user, provider__enabled=True).first()
            if box is None or mail_id != str(box.pk):
                return HttpResponse(translate_text("Sua caixa mudou. Recarregue a página antes de salvar."), status=409)
            if ("send_links", True) in mail_edits and not mail_write_access(cursor, link.tenant):
                return HttpResponse(translate_text("Seu papel permite apenas leitura."), status=403)
            mail_changes.extend((box, link, relation, checked) for relation, checked in mail_edits)
        if not legacy and initial == state_key(desired):
            continue
        if mode == "selected" and selected - {str(space) for space,_,_ in rows}:
            return HttpResponseBadRequest(translate_text("Selecione apenas espaços disponíveis nesta instituição."))
        current = None if selection(link) is None else [str(space) for space in effective(link, rows)]
        if not legacy and initial != state_key(current) and state_key(desired) != state_key(current):
            return HttpResponse(translate_text("As permissões mudaram em outra sessão. Recarregue a página e revise suas escolhas."), status=409)
        changes.append((link, desired))
    for link, desired in changes:
        WorkspaceSelection.objects.update_or_create(link=link, defaults={"spaces":desired})
    for box, link, relation, checked in mail_changes:
        grants = getattr(box, relation)
        if checked:
            grants.add(link)
        else:
            grants.remove(link)
        if relation == "links" and link.offer_mail_access:
            link.offer_mail_access = False
            link.save(update_fields=["offer_mail_access"])
    return redirect(request.path + "?salvo=1")


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def manage(request):
    with transaction.atomic(), connection.cursor() as cursor:
        principal, owner = bind_account(cursor, request.user)
        if not principal:
            raise Http404
        internal_mail = mail_access.allowed(cursor, mail_access.INSTITUTION)
        links = mail_access.visible_links(Link.objects.filter(user=request.user, protocol="mcp"), internal_mail)
        if request.method == "POST":
            return save_permissions(request, cursor, links)
        groups = {}
        internal_box = (Mailbox.objects.filter(user=request.user, provider_id=mail_access.INSTITUTION, provider__enabled=True)
               .prefetch_related("links", "send_links").first()) if internal_mail else None
        recent = CatalogObservation.objects.filter(link__user=request.user, listed_at__isnull=False).order_by(
            "link_id", "-listed_at").distinct("link_id").values_list("link_id", "listed_at", "token__scope")
        observations = {key:(stamp, "hub.mail.send" in scope.split()) for key, stamp, scope in recent}
        send_access = {}
        entries = []
        for link in links.select_related("workspaceselection").order_by("name","pk"):
            if link.tenant not in groups:
                groups[link.tenant] = visible(cursor, link.tenant)
            rows = groups[link.tenant]
            if not rows:
                continue
            ids = effective(link, rows)
            all_spaces = selection(link) is None
            box = internal_box if link.tenant == mail_access.INSTITUTION else None
            if box and link.tenant not in send_access:
                send_access[link.tenant] = mail_write_access(cursor, link.tenant)
            entries.append({"link":link, "observed":observations.get(link.pk), "box":box, "can_enable_send":send_access.get(link.tenant),
                            "read_mail":bool(box and link in box.links.all()),
                            "send_mail":bool(box and link in box.send_links.all()), "organization":rows[0][2], "tenant":link.tenant,
                            "mode":"all" if all_spaces else "selected" if ids else "paused",
                            "count":len(ids), "initial":state_key(None if all_spaces else map(str, ids)),
                            "choices":[(space,name,space in ids) for space,name,_ in rows]})
        entries.sort(key=lambda item: (item["organization"].casefold(), item["tenant"], item["link"].name.casefold()))
    return render(request, "ai_workspaces.html", {"entries":entries, "internal_mail":internal_mail, "saved":request.GET.get("salvo")=="1", "is_owner":owner})
