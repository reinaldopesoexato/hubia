"""Manage personal IAves subscriptions and read the portal inbox."""
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as tr
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from bridge import avihub, iaves
from bridge.iaves_subscriptions import Frequency, Kind, MailStatus, Status, cadence, eligible_email, linked_accounts, target_choices
from bridge.models import IAvesNotice, IAvesSubscription
from iaves_portal import agent_context


def change_subscription(request, entries, tenant, space, agent):
    try:
        ident = UUID(request.POST.get("subscription", ""))
        entry = entries.select_for_update().get(pk=ident)
    except (ValueError, IAvesSubscription.DoesNotExist):
        raise Http404 from None
    action = request.POST.get("action")
    if action == "delete":
        entry.delete()
    elif action == "pause":
        entry.active = False
        entry.save(update_fields=["active"])
        IAvesNotice.objects.filter(subscription=entry, mail_status=MailStatus.PENDING).update(mail_status=MailStatus.SKIPPED)
    else:
        raise Http404
    return redirect("iaves_subscriptions", tenant, space, agent)


def save_subscription(user, space, session, choices, params):
    kind, unit, nucleus, batch, frequency, channel = (
        params.get(field, "") for field in ("kind", "unit", "nucleus", "batch", "frequency", "channel"))
    if kind not in Kind.values or frequency not in Frequency.values or channel not in ("portal", "email"):
        raise ValueError(tr("Escolha um tipo, uma frequência e um canal para o aviso."))
    selected = next((item for item in choices["units"] if item["id"] == unit and item["ativa"]), None)
    if not selected or (kind == Kind.SUMMARY and batch not in choices["batches"]):
        raise ValueError(tr("Escolha uma unidade ativa e, para um resumo, selecione o lote."))
    if kind == Kind.OFFLINE:
        nucleus = batch = ""
    email = eligible_email(user, session) if channel == "email" else ""
    if channel == "email" and not email:
        raise ValueError(tr("O e-mail exige o mesmo endereço nas contas HUB IA e AviHUB e um servidor de envio configurado."))
    now = timezone.now()
    next_due = now + cadence(frequency) if kind == Kind.SUMMARY else now
    entry, created = IAvesSubscription.objects.select_for_update().get_or_create(
        user=user, space=space, session=session, kind=kind, unit=unit, nucleus=nucleus, batch=batch,
        defaults={"unit_label": selected["nome"][:160], "batch_label": choices["batches"].get(batch, "")[:160],
                  "frequency": frequency, "delivery_email": email, "next_due": next_due})
    if not created:
        if entry.delivery_email != email:
            IAvesNotice.objects.filter(subscription=entry, mail_status=MailStatus.PENDING).update(mail_status=MailStatus.SKIPPED)
        if entry.frequency != frequency or not entry.active:
            entry.next_due = next_due
            entry.status = Status.WAITING
        entry.delivery_email = email
        entry.frequency = frequency
        entry.active = True
        entry.save(update_fields=["status", "next_due", "delivery_email", "frequency", "active"])


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def subscriptions(request, tenant, space, agent):
    params = request.POST if request.method == "POST" else request.GET
    ref, kind, unit, nucleus = (params.get(field, "") for field in ("account", "kind", "unit", "nucleus"))
    kind = kind or Kind.SUMMARY
    error = None
    choices = {"units": [], "nuclei": {}, "batches": {}}
    email = ""
    with transaction.atomic(), connection.cursor() as cursor:
        ctx, record = agent_context(cursor, request.user, tenant, space, agent)
        location = iaves.avihub_location(cursor, ctx)
        ready = linked_accounts(request.user, location)
        entries = IAvesSubscription.objects.filter(user=request.user, space=space)
        if request.method == "POST" and params.get("action") != "save":
            return change_subscription(request, entries, tenant, space, agent)
        if ref:
            try:
                session = avihub.locked_session(ready, ref)
                choices = avihub.use_session(session, target_choices, unit, nucleus if kind == Kind.SUMMARY else "",
                                             kind == Kind.SUMMARY)
                email = eligible_email(request.user, session)
                if request.method == "POST":
                    save_subscription(request.user, space, session, choices, params)
                    return redirect("iaves_subscriptions", tenant, space, agent)
            except ValueError as exc:
                error = str(exc)
        elif request.method == "POST":
            error = tr("Escolha uma conta AviHUB vinculada.")

        available = list(ready.order_by("label", "pk"))
        notices = IAvesNotice.objects.filter(subscription__in=entries.filter(session__in=available))
        ctx.update(agent=record, available=available, linked=next((item for item in available if item.account_ref == ref), None),
                   selected_kind=kind, selected_unit=unit, selected_nucleus=nucleus,
                   choices=choices, eligible=email, error=error,
                   entries=list(entries.select_related("session").order_by("unit_label", "kind", "batch_label")),
                   notices=list(notices.order_by("-created_at", "-pk")[:30]))
        if location:
            ctx["link_avihub"] = reverse("use_agent", args=[tenant, *location]) + "?nova=1"
    return render(request, "iaves_subscriptions.html", ctx, status=400 if error else 200)
