"""Queue, access and authenticated delivery of AviHUB Peso Aves reports."""
import hashlib
import json
import logging
from datetime import timedelta
from time import sleep
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.db import close_old_connections, connection, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as tr

from model_provider import cipher
from space_views import active_agent, space_context
from . import avihub, avihub_api, avihub_report_api as native, avihub_report_delivery as delivery
from .access import ORIGIN, context as link_context
from .models import AviHubReport, Link


logger = logging.getLogger(__name__)
State, Delivery = AviHubReport.State, AviHubReport.Delivery
PUBLIC_STATES = {State.PENDING: "pendente", State.RUNNING: "gerando", State.WAITING: "aguardando_notificacao",
                 State.READY: "pronto", State.PREVIEW: "somente_previa",
                 State.FAILED: "falhou", State.UNCERTAIN: "incerto"}
UNCERTAIN = "O AviHUB não confirmou a criação. Confira as notificações do AviHUB antes de gerar outro relatório."
UNCONFIRMED_FILE = "O AviHUB recebeu a solicitação, mas o arquivo e a notificação correspondente ainda não foram confirmados."
PREVIEW_ONLY = "A prévia está disponível no AviHUB. O Hub IA não conseguiu salvar o PDF."
CONFIRM_TIMEOUT = timedelta(minutes=5)
CONFIRM_INTERVAL = timedelta(seconds=8)


class SessionBusy(Exception):
    """The linked account is handling another operation in the Hub."""


def authorize(cursor, tenant, space, agent_id, user, *, link=None, can_notify=False):
    if not user.is_active:
        raise Http404
    ctx = {}
    if link is not None:
        if link.user_id != user.pk or link.protocol != "mcp" or link.email_only:
            raise Http404
        ctx = link_context(link, multi_space=True)
        if ctx.get("space_tenants", {}).get(space) != tenant:
            raise Http404
        ctx["can_notify"] = can_notify
    ctx.update(space_context(cursor, user, tenant, space))
    agent = active_agent(cursor, tenant, space, agent_id)
    if agent["kind"] != "avihub" or not avihub.AviHubAgent.objects.filter(pk=agent_id, tenant=tenant).exists():
        raise Http404
    return ctx


def report_params(args):
    params = {key: args.get(key, "").strip() for key in ("unidade", *native.FILTER_FIELDS) if args.get(key)}
    if any(len(value) > 200 or not value.isprintable() for value in params.values()):
        raise ValueError(tr("Confira os filtros do relatório de Peso Aves."))
    return params


def open_form(tenant, space, agent_id, user, args, *, for_generation=False):
    url = ORIGIN + reverse("use_agent", args=[tenant, space, agent_id])
    session, context = avihub.linked_account(tenant, user, agent_id, args.get("conta"), link_url=url)
    if session is None:
        return None, context
    try:
        result = avihub.use_session(session, lambda panel: native.prepare(
            panel, report_params(args), for_generation=for_generation,
        ))
    except avihub.SessionExpired:
        return None, avihub.linking_help(url + "?nova=1", expired=True)
    return session, {**context, **result}


@transaction.atomic
def options(tenant, space, agent_id, user, args, *, link=None, can_notify=False):
    with connection.cursor() as cursor:
        ctx = authorize(cursor, tenant, space, agent_id, user, link=link, can_notify=can_notify)
        session, result = open_form(tenant, space, agent_id, user, args)
        if session is not None and result.get("estado") == "opcoes":
            result["canais"] = delivery.options(cursor, user, ctx, session, link=link, report_format=args.get("formato"))
        return result


def report_view(report):
    route = [report.tenant, report.space, report.agent_id]
    summary = {"estado":PUBLIC_STATES[report.state], "relatorio_id":str(report.pk),
               "criado_em":report.created.isoformat(), "formato":report.parameters["formato"],
               "inicio":report.parameters["inicio"], "fim":report.parameters["fim"], "unidade":report.parameters["unidade"],
               "unidade_nome":report.parameters.get("unidade_nome", ""), "canal":report.channel,
               "formato_nome":report.parameters.get("formato_nome", ""),
               "entrega":report.delivery, "entrega_label":tr(report.get_delivery_display()),
               "url_relatorio":ORIGIN + reverse("avihub_reports", args=route) + f"?relatorio_id={report.pk}#relatorios"}
    if report.state in (State.WAITING, State.READY, State.PREVIEW, State.UNCERTAIN):
        summary["url_notificacoes_avihub"] = report.agent.origin + "/notifications"
    if report.native_notification_id:
        summary["notificacao_avihub_id"] = report.native_notification_id
    if report.state in (State.READY, State.PREVIEW):
        url = ORIGIN + reverse("avihub_report_download", args=[*route, report.pk])
        if report.state == State.PREVIEW:
            summary.update(modo="visualizacao", url_previa=url, mensagem=tr(PREVIEW_ONLY))
        elif summary["formato"] in native.PDF_FORMATS and not report.file_name:
            summary.update(modo="visualizacao", url_download=url)
            summary["mensagem"] = tr("Abra a prévia no AviHUB e use a impressão para salvar em PDF.")
        else:
            source = "Prévia do AviHUB" if report.file_from_preview else "AviHUB"
            summary.update(modo="arquivo", url_download=url, fonte=source)
            if report.file_name:
                summary.update(arquivo_nome=report.file_name, arquivo_tipo=report.file_type, arquivo_tamanho=report.file_size)
    elif report.error:
        summary["mensagem"] = report.error
    return summary


@transaction.atomic
def enqueue(tenant, space, agent_id, user, args, *, link=None, can_notify=False):
    with connection.cursor() as cursor:
        ctx = authorize(cursor, tenant, space, agent_id, user, link=link, can_notify=can_notify)
        try:
            request_id = UUID(args.get("request_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError(tr("Identificador de solicitação do relatório inválido.")) from None
        channel = args.get("canal") or "painel"
        destination = args.get("destino", "").strip()
        intent = [str(tenant), str(space), str(agent_id), args.get("conta", "").strip(),
                  report_params(args), channel, destination]
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        previous = AviHubReport.objects.defer("printed_pdf").filter(user=user, request_id=request_id).first()
        if previous:
            return matching_request(previous, fingerprint)

        session, result = open_form(tenant, space, agent_id, user, args, for_generation=True)
        if session is None or result.get("estado") != "opcoes":
            return result
        params = result["parametros"]
        destination = delivery.validate(cursor, user, ctx, session, channel, destination,
                                        link=link, report_format=params["formato"])
        format_name = next(item["nome"] for item in result["formatos"] if item["id"] == params["formato"])
        report, _ = AviHubReport.objects.get_or_create(user=user, request_id=request_id, defaults={
            "tenant":tenant, "space":space, "agent_id":agent_id, "session":session, "link":link,
            "from_mcp":link is not None, "fingerprint":fingerprint,
            "parameters":{**params, "unidade_nome":result["unidade"]["nome"], "formato_nome":format_name},
            "channel":channel, "destination":destination,
        })
        return matching_request(report, fingerprint)


def matching_request(report, fingerprint):
    if report.fingerprint != fingerprint:
        raise ValueError(tr("Este identificador já foi usado para outro relatório."))
    return report_view(report)


@transaction.atomic
def status(tenant, space, agent_id, user, args):
    with connection.cursor() as cursor:
        authorize(cursor, tenant, space, agent_id, user)
    reports = AviHubReport.objects.defer("printed_pdf").filter(user=user, tenant=tenant, space=space,
                                                              agent_id=agent_id).select_related("agent")
    if value := args.get("relatorio_id"):
        try:
            report = reports.filter(pk=UUID(value)).first()
        except (ValueError, TypeError, AttributeError):
            report = None
        if report is None:
            raise ValueError(tr("Relatório de Peso Aves indisponível."))
        return report_view(report)
    return {"relatorios":[report_view(report) for report in reports.order_by("-created", "-pk")[:30]]}


def report_session(report, *, skip_locked=False):
    visible = avihub.personal(report.tenant, report.user).filter(
        pk=report.session_id, agent_id=report.agent_id, ready=True,
    )
    session = visible.select_related("agent").select_for_update(of=("self",), skip_locked=skip_locked).first()
    if session is None:
        if skip_locked and visible.exists():
            raise SessionBusy
        raise avihub_api.AccessDenied(tr("Vincule a conta AviHUB que solicitou o relatório."))
    return session


def fetch_file(report, *, skip_locked=False):
    session = report_session(report, skip_locked=skip_locked)
    unit_id = report.parameters["unidade"]
    if not report.file_from_preview:
        return avihub.use_session(session, native.download, unit_id, report.native_id, report.parameters["formato"])
    avihub.use_session(session, native.require_download_access, unit_id)
    try:
        data = cipher().decrypt(bytes(report.printed_pdf or b""))
    except InvalidToken:
        raise ValueError(tr("O PDF salvo está indisponível.")) from None
    return report.file_name, report.file_type, data


def collect_file(report, session):
    unit, output_format = report.parameters["unidade"], report.parameters["formato"]
    try:
        return avihub.use_session(session, native.download, unit, report.native_id, output_format)
    except native.NativePdfUnavailable:
        try:
            file = avihub.use_session(session, native.print_preview, unit, report.native_id)
        except ValueError as exc:
            raise native.NativePdfUnavailable from exc
        report.printed_pdf = cipher().encrypt(file[2])
        report.file_from_preview = True
        return file


@transaction.atomic
def read_file(tenant, space, agent_id, user, report_id, *, link=None):
    with connection.cursor() as cursor:
        authorize(cursor, tenant, space, agent_id, user, link=link)
    report = AviHubReport.objects.filter(pk=report_id, user=user, tenant=tenant, space=space, agent_id=agent_id,
                                        state=State.READY).first()
    if report is None or (link is not None and not report.file_name):
        raise Http404
    return fetch_file(report)


@transaction.atomic
def download(tenant, space, agent_id, user, report_id):
    with connection.cursor() as cursor:
        authorize(cursor, tenant, space, agent_id, user)
    report = AviHubReport.objects.filter(pk=report_id, user=user, tenant=tenant, space=space, agent_id=agent_id,
                                        state__in=(State.READY, State.PREVIEW)).first()
    if report is None:
        raise Http404
    if report.parameters["formato"] in native.PDF_FORMATS and not report.file_name:
        url = avihub.use_session(report_session(report), native.preview_url, report.parameters["unidade"], report.native_id)
        return redirect(url)
    filename, mime, data = fetch_file(report)
    response = HttpResponse(data, content_type=mime)
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def current_link(report):
    if not report.from_mcp:
        return None
    link = Link.objects.filter(pk=report.link_id, user_id=report.user_id, protocol="mcp").first()
    if link is None:
        raise Http404
    return link


def generate_next(*, report_id=None):
    now = timezone.now()
    with transaction.atomic():
        reports = AviHubReport.objects.all() if report_id is None else AviHubReport.objects.filter(pk=report_id)
        reports.filter(state=State.RUNNING, started_at__lt=now - timedelta(minutes=10)).update(
            state=State.UNCERTAIN, error=UNCERTAIN,
        )
        report = reports.select_for_update(skip_locked=True).filter(state=State.PENDING).order_by("created", "pk").first()
        if report is None:
            return False
        report.state, report.started_at = State.RUNNING, now
        report.save(update_fields=["state", "started_at"])

    with transaction.atomic(), connection.cursor() as cursor:
        report = AviHubReport.objects.select_for_update().filter(pk=report.pk, state=State.RUNNING).first()
        if report is None:
            return True
        try:
            authorize(cursor, report.tenant, report.space, report.agent_id, report.user, link=current_link(report))
            session = report_session(report, skip_locked=True)
            report.native_id = avihub.use_session(session, native.generate, report_params(report.parameters))
        except SessionBusy:
            report.state, report.started_at = State.PENDING, None
            report.save(update_fields=["state", "started_at"])
            return False
        except Http404:
            report.state, report.error = State.FAILED, tr("O acesso ao agente ou à conexão foi revogado.")
        except native.SubmissionUncertain:
            report.state, report.error = State.UNCERTAIN, UNCERTAIN
        except ValueError as exc:
            report.state, report.error = State.FAILED, str(exc)[:300]
        else:
            report.state, report.next_check_at = State.WAITING, timezone.now()
        report.save(update_fields=["state", "error", "native_id", "next_check_at"])

    if report.state == State.WAITING:
        confirm_next(report_id=report.pk)
    return True


@transaction.atomic
def confirm_next(*, report_id=None):
    now = timezone.now()
    pending = AviHubReport.objects.filter(state=State.WAITING, next_check_at__lte=now)
    if report_id is not None:
        pending = pending.filter(pk=report_id)
    report = pending.select_for_update(skip_locked=True).order_by("next_check_at", "pk").first()
    if report is None:
        return False

    report.next_check_at = None
    unit_id = report.parameters["unidade"]
    timed_out = now - report.started_at >= CONFIRM_TIMEOUT
    try:
        with connection.cursor() as cursor:
            authorize(cursor, report.tenant, report.space, report.agent_id, report.user, link=current_link(report))
        session = report_session(report, skip_locked=True)
        if not report.native_notification_id:
            report.native_notification_id = avihub.use_session(session, native.find_notification, unit_id, report.native_id) or ""
        if report.native_notification_id:
            report.file_name, report.file_type, data = collect_file(report, session)
            report.file_size = len(data)
            report.state = State.READY
            if report.channel != "painel":
                report.delivery = Delivery.PENDING
    except SessionBusy:
        report.next_check_at = now + CONFIRM_INTERVAL
        report.save(update_fields=["next_check_at"])
        return False
    except Http404:
        report.state, report.error = State.UNCERTAIN, tr("O acesso ao agente ou à conexão foi revogado.")
    except native.NativePdfUnavailable:
        # Keep the native preview accessible without presenting it as a downloadable file.
        if timed_out:
            report.state = State.PREVIEW
            if report.channel != "painel":
                report.delivery = Delivery.FAILED
    except ValueError:
        # A failed GET is safe to retry. The original submission must stay single-shot.
        pass

    if report.state == State.WAITING:
        if timed_out:
            report.state, report.error = State.UNCERTAIN, tr(UNCONFIRMED_FILE)
        else:
            report.next_check_at = now + CONFIRM_INTERVAL
    report.save(update_fields=["state", "error", "next_check_at", "native_notification_id", "file_name", "file_type",
                               "file_size", "file_from_preview", "printed_pdf", "delivery"])
    return True


def deliver_next(*, report_id=None):
    with transaction.atomic():
        pending = AviHubReport.objects.filter(state=State.READY, delivery=Delivery.PENDING)
        if report_id is not None:
            pending = pending.filter(pk=report_id)
        report = pending.select_for_update(skip_locked=True).order_by("created", "pk").first()
        if report is None:
            return False
        report.delivery = Delivery.UNCERTAIN
        report.save(update_fields=["delivery"])

    with transaction.atomic(), connection.cursor() as cursor:
        report = AviHubReport.objects.select_for_update().filter(pk=report.pk).first()
        if report is None:
            return True
        attachment = None
        try:
            authorize(cursor, report.tenant, report.space, report.agent_id, report.user, link=current_link(report))
            if report.channel == "email_anexo":
                attachment = fetch_file(report, skip_locked=True)
        except SessionBusy:
            report.delivery = Delivery.PENDING
            report.save(update_fields=["delivery"])
            return False
        except Http404:
            outcome = Delivery.SKIPPED
        except ValueError:
            outcome = Delivery.FAILED
        else:
            outcome = delivery.deliver(report, attachment=attachment)
        report.delivery = outcome
        report.save(update_fields=["delivery"])
    return True


def run():
    while True:
        close_old_connections()
        try:
            generated = generate_next()
            confirmed = confirm_next()
            notified = deliver_next()
        except Exception as exc:
            logger.error("Falha no worker de relatórios AviHUB (%s).", type(exc).__name__)
        else:
            if generated or confirmed or notified:
                continue
        sleep(5)
