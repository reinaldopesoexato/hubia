"""Scheduled, account-scoped reads of the AviHUB panel."""
import logging
import re
from datetime import timedelta
from decimal import Decimal
from time import sleep

from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import close_old_connections, connection, transaction
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from . import avihub, avihub_api, iaves
from .models import IAVES_AGENT_ID, PESO_EXATO_TENANT_ID, IAvesNotice, IAvesSubscription, PlatformMailSettings
from .platform_email import MailUnavailable

logger = logging.getLogger(__name__)
Kind, Frequency, Status = IAvesSubscription.Kind, IAvesSubscription.Frequency, IAvesSubscription.Status
MailStatus = IAvesNotice.MailStatus
MAX_PAGES = 8
EQUIPMENT_MODULE = "/equipment"
CARD_WEIGHT = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(kg|g)", re.IGNORECASE)
UNASSIGNED_BATCHES = {"Lote Não informado", "Batch Not informed", "Lote No informado"}


def cadence(frequency):
    return timedelta(days=7 if frequency == Frequency.WEEKLY else 1)


def context_for(sub):
    from iaves_portal import agent_context

    if not sub.user.is_active:
        raise Http404
    with connection.cursor() as cursor:
        ctx, _ = agent_context(cursor, sub.user, PESO_EXATO_TENANT_ID, sub.space, IAVES_AGENT_ID)
        return iaves.avihub_location(cursor, ctx)


def linked_accounts(user, location):
    accounts = avihub.personal(PESO_EXATO_TENANT_ID, user).filter(ready=True)
    return accounts.filter(agent_id=location[1]) if location else accounts.none()


def eligible_email(user, session):
    # A user may edit their Hub IA address. Require it to match their authenticated AviHUB account.
    if not user.is_active or not user.email or user.email.casefold() != session.email.casefold():
        return ""
    try:
        validate_email(user.email)
    except ValidationError:
        return ""
    smtp = PlatformMailSettings.objects.filter(pk=1, tls_mode__in=["ssl", "starttls"]).first()
    return user.email if smtp and smtp.encrypted_password else ""


def target_choices(panel, unit_id="", nucleus_id="", include_flocks=True):
    doc = panel.home()
    units = panel.units(doc)
    choices = {"units": units, "nuclei": {}, "batches": {}}
    if not unit_id:
        return choices
    unit = next((item for item in units if item["id"] == unit_id and item["ativa"]), None)
    if not unit:
        raise avihub_api.AccessDenied("Escolha a unidade ativa no painel AviHUB.")
    panel.require_context(doc, unit_id, EQUIPMENT_MODULE)
    if not include_flocks:
        return choices
    scope = {"units": unit_id}
    choices["nuclei"] = panel.options("nuclei_id", scope)
    if nucleus_id:
        nucleus = choices["nuclei"].get(nucleus_id)
        if not nucleus:
            raise avihub_api.AccessDenied("Escolha um núcleo listado nesta unidade.")
        batches = panel.options("batch_id", {**scope, "nucleis": nucleus_id})
        choices["batches"] = {key: label for key, label in batches.items()
                              if label.removesuffix(f"({nucleus})") not in UNASSIGNED_BATCHES}
    return choices


def weight_in_kg(equipment, value):
    if equipment.upper().startswith("CSFC/"):
        return None
    match = CARD_WEIGHT.fullmatch(value.strip())
    if match:
        amount = Decimal(match[1].replace(",", "."))
        if amount > 0:
            return amount if match[2].lower() == "kg" else amount / 1000
    return None


def summarize(panel, sub, due):
    choices = target_choices(panel, sub.unit, sub.nucleus)
    nucleus = choices["nuclei"].get(sub.nucleus)
    selected_batch = choices["batches"].get(sub.batch)
    if not selected_batch or not nucleus:
        raise avihub_api.AccessDenied("Lote indisponível neste núcleo.")
    # The selector renders Batch#number_and_nuclei, while the table shows Batch#valid_number.
    batch = selected_batch.removesuffix(f"({nucleus})")

    # The basic /reports timeline accepts only nucleus, so query the native bird weights list.
    effective_day = timezone.localdate(due)
    start = effective_day - cadence(sub.frequency)
    end = effective_day - timedelta(days=1)
    path = "/weights"
    filters = {"nuclei_id": sub.nucleus, "batch_id": sub.batch, "start_date": start.isoformat(), "end_date": end.isoformat()}
    following, total = 1, 0
    mass = []
    for _ in range(MAX_PAGES):
        doc = panel.page(path, {**filters, "page": following}, sub.unit, EQUIPMENT_MODULE)
        form = doc.find("form", action=path)
        for field in ("start_date", "end_date"):
            entry = form and form.find("input", attrs={"name": field})
            if not entry or entry.get("value") != filters[field]:
                raise avihub_api.AccessDenied("O AviHUB não confirmou as datas do relatório.")
        cols, records = avihub_api.table(doc)
        weight_columns = [idx for idx, label in enumerate(cols) if label.casefold() in {"pesagem", "weight", "pesaje"}]
        for row in records:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(recursive=False)]
            if len(cells) != len(cols) or len(cells) < 4 or cells[1:3] != [nucleus, batch]:
                raise avihub_api.AccessDenied("O AviHUB não confirmou o lote e o núcleo do relatório.")
            if len(weight_columns) == 1:
                amount = weight_in_kg(cells[0], cells[weight_columns[0]])
                if amount is not None:
                    mass.append(amount)
        total += len(records)
        following = avihub_api.next_page(doc, following)
        if not following:
            break
    count = "1 registro de pesagem." if total == 1 else f"{total} registros de pesagem."
    if following:
        count = f"Consulta parcial de até {MAX_PAGES} páginas. Pelo menos {count}"
    if mass:
        mean = f"{sum(mass) / len(mass):.3f}".replace(".", ",")
        count += f"\nMédia dos valores de pesagem exibidos: {mean} kg ({len(mass)} de {total} registros)."
    elif total:
        count += "\nValores comparáveis de peso indisponíveis nesta consulta."
    unit = next(item["nome"] for item in choices["units"] if item["id"] == sub.unit)
    return "Resumo de pesagens do lote", (
        f"{unit} · {nucleus} · Lote {batch}\n"
        f"{start:%d/%m/%Y} a {end:%d/%m/%Y}\n{count}\nFonte: AviHUB, Pesagens de aves.")


def check_equipment(panel, sub):
    doc = panel.home()
    unit = avihub_api.select_unit(panel.units(doc), sub.unit)
    panel.require_context(doc, sub.unit, EQUIPMENT_MODULE)
    following, current = 1, {}
    for _ in range(MAX_PAGES):
        result = avihub_api.equipment_query(panel, unit, "equipamentos_sem_comunicacao", {"horas": "24"}, following)
        current.update((item["id"], item) for item in result["equipamentos"])
        following = result["proxima_pagina"]
        if not following:
            break
    previous = set(sub.offline_ids)
    new = [item for key, item in current.items() if key not in previous]
    # A partial scan cannot distinguish recovery from a device outside the pages returned.
    sub.offline_ids = sorted(previous | current.keys() if following else current)
    if not new:
        return None
    lines = [f"{item['modelo']} / {item['serie']} · {item['localizacao'] or 'Local indisponível'} · "
             f"Última conexão: {item['ultima_conexao'] or 'Data indisponível'}" for item in new]
    info = f"{unit['nome']} · {len(new)} equipamento(s) detectado(s) sem comunicação há 24 horas ou mais."
    if following:
        lines.append(f"A consulta de equipamentos foi limitada a {MAX_PAGES} páginas.")
    return "Equipamentos sem comunicação", "\n".join([info, *lines, "Fonte: AviHUB, Equipamentos."])


@transaction.atomic
def check_due(*, now=None, subscription_id=None):
    now = timezone.now() if now is None else now
    due = IAvesSubscription.objects.filter(active=True, next_due__lte=now)
    if subscription_id is not None:
        due = due.filter(pk=subscription_id)
    sub = (due.select_related("user").select_for_update(skip_locked=True, of=("self",))
           .order_by("next_due", "pk").first())
    if sub is None:
        return False

    interval = cadence(sub.frequency)
    note = None
    try:
        accounts = linked_accounts(sub.user, context_for(sub))
        available = accounts.filter(pk=sub.session_id)
        session = available.select_related("agent").select_for_update(skip_locked=True, of=("self",)).first()
        if session is None:
            if available.exists():
                return False
            raise avihub_api.AccessDenied("Conta desvinculada ou indisponível.")
        periods_elapsed = (now - sub.next_due) // interval
        window = sub.next_due + periods_elapsed * interval
        if sub.kind == Kind.SUMMARY:
            note = avihub.use_session(session, summarize, sub, window)
        else:
            note = avihub.use_session(session, check_equipment, sub)
        sub.status = Status.READY
    except (Http404, avihub_api.AccessDenied, avihub_api.SessionExpired):
        sub.status = Status.BLOCKED
    except ValueError:
        sub.status = Status.UNAVAILABLE
    if note:
        IAvesNotice.objects.get_or_create(subscription=sub, scheduled_for=window, defaults={
            "title": note[0], "body": note[1], "mail_status": MailStatus.PENDING if sub.delivery_email else MailStatus.PORTAL,
        })
    if sub.session_id and not avihub.AviHubSession.objects.filter(pk=sub.session_id).exists():
        sub.session_id = None
    sub.next_due += (((now - sub.next_due) // interval) + 1) * interval
    sub.save(update_fields=["status", "next_due", "offline_ids"])
    return True


def deliver_notice():
    # Commit the reservation before SMTP. If the outcome cannot be proven, never replay it.
    with transaction.atomic():
        notice = (IAvesNotice.objects.select_for_update(skip_locked=True).filter(mail_status=MailStatus.PENDING)
                  .order_by("created_at", "pk").first())
        if notice is None:
            return False
        notice.mail_status = MailStatus.UNCERTAIN
        notice.save(update_fields=["mail_status"])
    with transaction.atomic():
        try:
            sub = IAvesSubscription.objects.select_related("user").select_for_update(of=("self",)).get(pk=notice.subscription_id)
            account = (linked_accounts(sub.user, context_for(sub)).filter(pk=sub.session_id)
                       .select_for_update(skip_locked=True, of=("self",)).first()) if sub.active else None
        except (Http404, IAvesSubscription.DoesNotExist):
            account = None
        recipient = eligible_email(sub.user, account) if account else ""
        if not recipient or recipient != sub.delivery_email:
            status = MailStatus.SKIPPED
        else:
            url = reverse("iaves_subscriptions", args=[PESO_EXATO_TENANT_ID, sub.space, IAVES_AGENT_ID])
            try:
                accepted = send_mail("Novo aviso do IAves", f"Consulte seu aviso no HUB IA:\nhttps://hubia.psxt.io{url}", None, [recipient])
            except MailUnavailable:
                accepted = 0
            status = MailStatus.ACCEPTED if accepted == 1 else MailStatus.UNCERTAIN
        IAvesNotice.objects.filter(pk=notice.pk).update(mail_status=status)
    return True


def run():
    while True:
        close_old_connections()
        try:
            processed = check_due()
            mailed = deliver_notice()
        except Exception as exc:
            logger.error("Falha no worker de avisos do IAves (%s).", type(exc).__name__)
        else:
            if processed or mailed:
                continue
        sleep(30)
