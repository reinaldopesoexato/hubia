import hashlib
import json
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from django import forms
from django.utils.translation import gettext as translate_text
from django.utils.translation import gettext_lazy as tr
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from model_provider import cipher
from notification_channels import CHANNELS, RECIPIENT_VALIDATORS
from space_views import space_context, rows
from .models import NotificationChannel, NotificationDestination


class WhatsAppForm(forms.Form):
    integration = forms.RegexField(label=tr("Identificador da integração Leadfinder"), regex=r"^[A-Za-z0-9_-]{1,80}$")
    key = forms.RegexField(label=tr("Chave da API"), required=False, regex=r"^[\x21-\x7e]{1,1024}$",
                           widget=forms.PasswordInput(attrs={"autocomplete":"new-password"}),
                           help_text=tr("Deixe em branco para manter a chave salva."))
    enabled = forms.BooleanField(label=tr("Habilitar WhatsApp"), required=False)


class DestinationForm(forms.Form):
    name = forms.CharField(label=tr("Nome do destino"), max_length=100)
    address = forms.RegexField(label=tr("Telefone ou ID do grupo"), regex=r"^[0-9]{1,100}(?:@g\.us|-group)?$",
                               help_text=tr("Use o identificador exato cadastrado na Leadfinder."))
    sector = forms.ChoiceField(label=tr("Quem pode enviar"), required=False)


def channels(tenant, space, agent):
    return NotificationChannel.objects.filter(tenant=tenant, space=space, agent=agent, channel__in=CHANNELS)


def visible_spaces(cursor, tenant):
    cursor.execute("SELECT id,name FROM hub_tenancy.workspaces WHERE tenant_id=%s ORDER BY name", [tenant])
    return cursor.fetchall()


def destinations(cursor, tenant, space, agent, permitted=None):
    sectors = [item[0] for item in visible_spaces(cursor, tenant)]
    if permitted is not None:
        sectors = [sector for sector in sectors if sector in permitted]
    return NotificationDestination.objects.filter(
        Q(sector=None) | Q(sector__in=sectors),
        channel__in=channels(tenant, space, agent).filter(enabled=True),
    ).select_related("channel").order_by("channel__channel","name")


def resolve_recipient(cursor, tenant, space, agent, data, permitted=None):
    channel = channels(tenant, space, agent).filter(enabled=True, channel=data.get("canal")).first()
    if channel is None:
        raise ValueError(translate_text("Canal de envio indisponível."))
    saved_id = data.get("destino")
    direct = data.get("destinatario")
    if bool(saved_id) == bool(direct):
        raise ValueError(translate_text("Informe um destinatário ou selecione um destino salvo."))
    allowed = destinations(cursor, tenant, space, agent, permitted).filter(channel=channel)
    if saved_id:
        if not str(saved_id).isascii() or not str(saved_id).isdigit() or len(str(saved_id)) > 18:
            raise ValueError(translate_text("Destino salvo inválido."))
        saved = allowed.filter(pk=saved_id).first()
        if not saved:
            raise ValueError(translate_text("Destino indisponível."))
        return channel, saved.address, saved.name, saved.pk
    validator = RECIPIENT_VALIDATORS.get(channel.channel)
    if not validator:
        raise ValueError(translate_text("Este canal exige um destino salvo."))
    address, equivalents = validator(direct)
    saved_addresses = channel.notificationdestination_set.filter(address__in=equivalents)
    if saved_addresses.exists() and not allowed.filter(address__in=equivalents).exists():
        raise ValueError(translate_text("Destino indisponível."))
    return channel, address, address, None


def send(cursor, user, ctx, agent, data, *, connection_name=None):
    tenant, space = ctx["tenant"], ctx["space"]
    cursor.execute("""SELECT name FROM hub_tenancy.workspace_agents WHERE tenant_id=%s AND workspace_id=%s
                      AND id=%s AND active AND kind='notifications'""", [tenant, space, agent])
    agent_row = cursor.fetchone()
    if not agent_row:
        raise ValueError(translate_text("Agente de notificações indisponível."))
    cursor.execute("""SELECT role FROM hub_tenancy.memberships
                      WHERE tenant_id=%s AND user_id=%s AND active""", [tenant, ctx["principal"]])
    role = cursor.fetchone()
    if not role or role[0]=="viewer":
        raise ValueError(translate_text("Seu papel não permite enviar avisos."))
    try:
        run = UUID(str(data.get("request_id","")))
    except (ValueError, TypeError):
        raise ValueError(translate_text("Identificador de envio inválido.")) from None
    message = data.get("mensagem")
    if not isinstance(message,str) or not 1 <= len(message.strip()) <= 2000:
        raise ValueError(translate_text("Informe uma mensagem de até 2.000 caracteres."))
    message = message.strip()
    binding, address, name, saved_id = resolve_recipient(cursor, tenant, space, agent, data, ctx.get("spaces"))
    channel = binding.channel
    try:
        config = json.loads(cipher().decrypt(bytes(binding.encrypted)))
    except InvalidToken:
        raise ValueError(translate_text("O administrador precisa salvar novamente a credencial deste canal.")) from None
    fingerprint = hashlib.sha256(json.dumps([channel, saved_id, address, message]).encode()).hexdigest()
    label = f"{CHANNELS[channel][0]} · {name}"
    cursor.execute("""INSERT INTO hub_tenancy.agent_runs
        (id,tenant_id,workspace_id,agent_id,user_id,question,answer,status,conversation_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'running',%s) ON CONFLICT DO NOTHING RETURNING id""",
        [run,tenant,space,agent,ctx["principal"],message,label,fingerprint])
    if cursor.fetchone():
        # A reserva é persistida antes do POST. Um timeout ou reinício nunca gera retry automático.
        outgoing = message
        if channel == "whatsapp":
            sender = " ".join((user.get_full_name() or user.get_username()).split())
            source = " ".join((connection_name or agent_row[0]).split())
            outgoing = f"_Enviado por {sender} através de {source}_\n\n{message}"
        def deliver():
            status, answer = CHANNELS[channel][1](config, address, outgoing)
            with transaction.atomic(), connection.cursor() as db:
                space_context(db,user,tenant,space)
                db.execute("UPDATE hub_tenancy.agent_runs SET answer=%s,status=%s WHERE id=%s",
                           [label+" · "+answer,status,run])
        transaction.on_commit(deliver)
    else:
        cursor.execute("""SELECT status,answer FROM hub_tenancy.agent_runs WHERE id=%s AND tenant_id=%s
            AND workspace_id=%s AND agent_id=%s AND user_id=%s AND conversation_key=%s""",
            [run,tenant,space,agent,ctx["principal"],fingerprint])
        prior = cursor.fetchone()
        if not prior:
            raise ValueError(translate_text("Identificador de envio já utilizado. Confira o histórico."))
        return {"id":str(run),"estado":prior[0],"resultado":prior[1]}
    return {"id":str(run),"estado":"registrado","destino":name}


@sensitive_post_parameters("key")
@login_required
@never_cache
@require_http_methods(["GET","POST"])
def configure(request, tenant, space, agent):
    with transaction.atomic(), connection.cursor() as cursor:
        ctx = space_context(cursor,request.user,tenant,space,management=True)
        cursor.execute("""SELECT name FROM hub_tenancy.workspace_agents WHERE tenant_id=%s AND workspace_id=%s
                          AND id=%s AND kind='notifications'""", [tenant,space,agent])
        found = cursor.fetchone()
        if not found:
            raise Http404
        ctx.update(agent_name=found[0],agent=agent)
        current = channels(tenant,space,agent).select_for_update().filter(channel="whatsapp").first()
        stored = {}
        if current:
            try:
                stored = json.loads(cipher().decrypt(bytes(current.encrypted)))
            except InvalidToken:
                pass
        action = request.POST.get("action")
        settings = WhatsAppForm(request.POST if action=="settings" else None,initial={
            "integration":stored.get("integration",""), "enabled":bool(current and current.enabled)})
        dest_form = DestinationForm(request.POST if action=="destination" else None)
        dest_form.fields["sector"].choices = [("","Todos com permissão de envio neste espaço"),
            *((str(pk),name) for pk,name in visible_spaces(cursor,tenant))]
        if request.method=="POST":
            if action=="settings" and settings.is_valid():
                data = settings.cleaned_data
                key = data["key"] or stored.get("key")
                if key:
                    encrypted = cipher().encrypt(json.dumps({"integration":data["integration"],"key":key}).encode())
                    channels(tenant,space,agent).update_or_create(tenant=tenant,space=space,agent=agent,
                        channel="whatsapp",defaults={"enabled":data["enabled"],"encrypted":encrypted})
                    return redirect("notification_config",tenant,space,agent)
                settings.add_error("key","Informe a chave da Leadfinder.")
            elif action=="destination" and dest_form.is_valid() and current:
                data = dest_form.cleaned_data
                _, created = NotificationDestination.objects.get_or_create(channel=current,name=data["name"],
                    defaults={"address":data["address"],"sector":data["sector"] or None})
                if created:
                    return redirect("notification_config",tenant,space,agent)
                dest_form.add_error("name","Já existe um destino com este nome.")
            elif action=="remove" and current and request.POST.get("destination","").isdigit():
                NotificationDestination.objects.filter(channel=current,pk=request.POST["destination"]).delete()
                return redirect("notification_config",tenant,space,agent)
            elif action not in ("settings","destination") or (action=="destination" and not current):
                return HttpResponseBadRequest(translate_text("Ação indisponível."))
        ctx.update(settings=settings,dest_form=dest_form,current=current,
                   targets=NotificationDestination.objects.filter(channel=current).order_by("name") if current else [])
    return render(request,"notification_config.html",ctx,status=400 if settings.errors or dest_form.errors else 200)
