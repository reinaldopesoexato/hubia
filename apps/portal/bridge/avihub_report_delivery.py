"""Delivery options for the requester of an AviHUB bird-weight report."""
import json
from email import encoders
from email.mime.base import MIMEBase

from cryptography.fernet import InvalidToken
from django.core.mail import EmailMessage
from django.db import connection, transaction
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as tr
from oauth2_provider.models import get_access_token_model

from model_provider import cipher
from notification_channels import CHANNELS
from space_views import active_agent, space_context
from . import avihub, notifications
from .access import ORIGIN, context as link_context
from .avihub_report_api import FORMATS, FORMAT_ALIASES
from .iaves_subscriptions import eligible_email
from .models import Link
from .platform_email import MailUnavailable


EMAIL_ATTACHMENT_LIMIT = 4 * 1024 * 1024
ATTACHMENT_TYPES = frozenset(mime for _, mime in FORMATS.values())


def whatsapp_destinations(cursor, ctx, link):
    if link is not None and not ctx.get("can_notify"):
        return []
    tenant, space = ctx["tenant"], ctx["space"]
    cursor.execute("SELECT set_config('hub.tenant_id', %s, true)", [str(tenant)])
    cursor.execute("""SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s AND user_id=%s AND active""",
                   [tenant, ctx["principal"]])
    role = cursor.fetchone()
    if not role or role[0] == "viewer":
        return []
    cursor.execute("""SELECT id FROM hub_tenancy.workspace_agents
        WHERE tenant_id=%s AND workspace_id=%s AND active AND kind='notifications' ORDER BY id""", [tenant, space])
    return [destination for (agent,) in cursor.fetchall() for destination in
            notifications.destinations(cursor, tenant, space, agent, ctx.get("spaces")).filter(channel__channel="whatsapp")]


def options(cursor, user, ctx, session, *, link=None, report_format=None):
    result = [{"canal":"painel", "nome":tr("Painel do HUB IA")}]
    email = eligible_email(user, session) if session is not None else ""
    if email:
        result.append({"canal":"email", "nome":tr("E-mail com link"), "destinatario":email})
        report_format = FORMAT_ALIASES.get(report_format, report_format)
        if report_format is None or report_format in FORMATS:
            result.append({"canal":"email_anexo", "nome":tr("E-mail com arquivo anexo"),
                           "destinatario":email, "limite_bytes":EMAIL_ATTACHMENT_LIMIT})
    destinations = whatsapp_destinations(cursor, ctx, link)
    if destinations:
        result.append({"canal":"whatsapp", "nome":"WhatsApp",
                       "destinos":[{"id":str(item.pk), "nome":item.name} for item in destinations]})
    return result


def whatsapp_destination(cursor, ctx, link, destination):
    if not destination or not destination.isascii() or not destination.isdigit() or len(destination) > 18:
        raise ValueError(tr("Selecione um destino de WhatsApp disponível para o relatório."))
    selected = next((item for item in whatsapp_destinations(cursor, ctx, link) if str(item.pk) == destination), None)
    if selected is None:
        raise ValueError(tr("O destino de WhatsApp está indisponível neste espaço."))
    return selected


def validate(cursor, user, ctx, session, channel, destination="", *, link=None, report_format=None):
    if channel == "painel" and not destination:
        return ""
    if channel in ("email", "email_anexo"):
        if channel == "email_anexo" and report_format not in FORMATS:
            raise ValueError(tr("O AviHUB não oferece este formato para envio em anexo."))
        email = eligible_email(user, session) if session is not None else ""
        if not email or (destination and destination.casefold() != email.casefold()):
            raise ValueError(tr("O e-mail precisa ser o mesmo nas contas HUB IA e AviHUB e ter envio configurado."))
        return email
    if channel == "whatsapp":
        return str(whatsapp_destination(cursor, ctx, link, destination).pk)
    raise ValueError(tr("Selecione um canal de entrega disponível para o relatório."))


def delivery_context(cursor, report):
    user = report.user
    user.refresh_from_db(fields=["is_active", "email"])
    if not user.is_active or not report.session_id or (report.from_mcp and not report.link_id):
        raise Http404
    session = avihub.personal(report.tenant, user).filter(pk=report.session_id, ready=True, agent_id=report.agent_id).first()
    if session is None:
        raise Http404

    link = None
    context = {}
    if report.link_id:
        link = Link.objects.filter(pk=report.link_id, user=user, protocol="mcp", email_only=False).first()
        if link is None:
            raise Http404
        context = link_context(link, multi_space=True)
        if context["space_tenants"].get(report.space) != report.tenant:
            raise Http404
        scopes = {"hub.read"}
        if report.channel == "whatsapp":
            if not link.writable:
                raise Http404
            scopes.add("hub.notify")
        grants = get_access_token_model().objects.filter(user=user, application__client_id=link.client_id,
                                                        expires__gt=timezone.now()).values_list("scope", flat=True)
        if not any(scopes <= set(value.split()) for value in grants):
            raise Http404
        context["can_notify"] = report.channel == "whatsapp"

    context.update(space_context(cursor, user, report.tenant, report.space))
    if active_agent(cursor, report.tenant, report.space, report.agent_id)["kind"] != "avihub":
        raise Http404
    return context, session, link


def build_attachment(attachment):
    if not isinstance(attachment, tuple) or len(attachment) != 3:
        return None
    filename, mime, data = attachment
    if not isinstance(mime, str) or not mime.isascii() or not mime.isprintable() or len(mime) > 150:
        return None
    content_type = mime.split(";", 1)[0].lower().strip()
    if (not isinstance(filename, str) or not 0 < len(filename) <= 255 or not filename.isprintable()
            or "/" in filename or "\\" in filename or content_type not in ATTACHMENT_TYPES
            or not isinstance(data, bytes) or not 0 < len(data) <= EMAIL_ATTACHMENT_LIMIT):
        return None
    # An exact copy matters for native CSV/TXT formats that may use non-UTF-8 encodings or CRLF.
    part = MIMEBase(*content_type.split("/", 1))
    part.replace_header("Content-Type", mime)
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    return part


def report_message(report):
    url = ORIGIN + reverse("avihub_report_download", args=[report.tenant, report.space, report.agent_id, report.pk])
    detail = tr("Abra o link para consultar o relatório no HUB IA.")
    return f"{tr('Seu relatório de Peso Aves do AviHUB está pronto.')}\n{detail}\n{url}"


@transaction.atomic
def deliver(report, *, attachment=None):
    """Try once after the worker commits the uncertain state, then report provider acceptance."""
    if report.state != "ready" or report.delivery != "uncertain" or not report.native_id:
        return "skipped"
    with connection.cursor() as cursor:
        try:
            ctx, session, link = delivery_context(cursor, report)
            if report.channel == "whatsapp":
                destination = whatsapp_destination(cursor, ctx, link, report.destination)
            else:
                email = validate(cursor, report.user, ctx, session, report.channel, report.destination,
                                 link=link, report_format=report.parameters.get("formato"))
                # A changed address stays blocked, even if it is otherwise eligible.
                if not email or email.casefold() != report.destination.casefold():
                    return "skipped"
        except (Http404, ValueError):
            return "skipped"

    body = report_message(report)
    if report.channel == "whatsapp":
        try:
            config = json.loads(cipher().decrypt(bytes(destination.channel.encrypted)))
            if not isinstance(config, dict) or not all(isinstance(config.get(key), str) for key in ("integration", "key")):
                return "failed"
            state, _ = CHANNELS["whatsapp"][1](config, destination.address, body)
        except (InvalidToken, ValueError, TypeError):
            return "failed"
        return "accepted" if state == "done" else "uncertain"

    message = EmailMessage(tr("Relatório Peso Aves disponível"), body, None, [email])
    if report.channel == "email_anexo":
        part = build_attachment(attachment)
        if part is None:
            return "failed"
        message.attach(part)
    try:
        return "accepted" if message.send() == 1 else "uncertain"
    except MailUnavailable:
        return "uncertain"
