"""Solicitação e consulta de arquivos da aba Peso Aves no portal."""
from uuid import UUID, uuid4

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as tr
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from bridge import avihub, avihub_report_api, avihub_reports
from bridge.models import AviHubAgent, AviHubReport
from space_views import active_agent, space_context


def agent_context(cursor, request, tenant, space, agent):
    ctx = space_context(cursor, request.user, tenant, space)
    record = active_agent(cursor, tenant, space, agent)
    if record["kind"] != "avihub" or not AviHubAgent.objects.filter(pk=agent, tenant=tenant).exists():
        raise Http404
    return ctx, record


def arguments(values):
    fields = ("conta", "unidade", "canal", "destino", *avihub_report_api.FILTER_FIELDS)
    result = {key:value for key in fields if (value := values.get(key, "").strip())}
    for field in avihub_report_api.SCOPED_FILTERS.keys() - {"nucleo_id"}:
        selected = [value.strip() for value in values.getlist(field) if value.strip()]
        if selected:
            result[field] = ",".join(selected)
    return result


def form_context(options, args):
    labels = {
        "nucleo_id":tr("Núcleo"), "lote_id":tr("Lote"), "galpao_id":tr("Galpão"),
        "lado_id":tr("Lado"), "box_id":tr("Box"), "equipamento_id":tr("Equipamento"),
        "sexo":tr("Sexo"), "tipo_balanca":tr("Tipo de balança"), "periodo_pesagem":tr("Período de pesagem"),
        "visualizacao":tr("Visualização"),
    }
    channels = {"painel":tr("Painel"), "email":tr("E-mail com link"),
                "email_anexo":tr("E-mail com anexo"), "whatsapp":tr("WhatsApp com link")}
    selected_account = options.get("conta") or {}
    selected_unit = options.get("unidade") or {}
    format_choices = options.get("formatos", [])
    filter_choices = options.get("filtros", {})
    channel_choices = options.get("canais", [])
    return {
        "accounts":options.get("contas") or ([selected_account] if selected_account else []),
        "units":options.get("unidades") or ([selected_unit] if selected_unit else []),
        "account_id":selected_account.get("id", args.get("conta")),
        "unit_id":selected_unit.get("id", args.get("unidade")),
        "formats":format_choices,
        "filters":[{"field":field, "label":label, "options":filter_choices[field],
                    "selected":args.get(field, "").split(","), "multiple":"," in args.get(field, "")}
                   for field, label in labels.items() if filter_choices.get(field)],
        "weighing_schedule":bool(filter_choices.get("periodo_pesagem")),
        "channels":[{**channel, "label":channels[channel["canal"]]}
                    for channel in channel_choices if channel.get("canal") in channels],
        "destinations":next((channel.get("destinos", []) for channel in channel_choices
                             if channel.get("canal") == "whatsapp"), []),
    }


def visible_report(row):
    labels = {"pendente":tr("Aguardando geração"), "gerando":tr("Gerando"), "pronto":tr("Pronto"),
              "aguardando_notificacao":tr("Aguardando a notificação do AviHUB"),
              "somente_previa":tr("Prévia disponível no AviHUB"),
              "falhou":tr("Falhou"), "incerto":tr("Aguardando confirmação do AviHUB")}
    delivery = row.get("entrega")
    choices = dict(AviHubReport.Delivery.choices)
    return {**row, "estado_label":labels.get(row.get("estado"), row.get("estado")),
            "entrega_label":tr(choices[delivery]) if delivery in choices and delivery != AviHubReport.Delivery.NONE else ""}


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def reports(request, tenant, space, agent):
    user = request.user
    params = request.POST if request.method == "POST" else request.GET
    args = arguments(params)
    destination = reverse("avihub_reports", args=(tenant, space, agent))
    request_id = uuid4()
    selected_id = request.GET.get("relatorio_id", "")
    error = None
    options = {}
    with transaction.atomic(), connection.cursor() as cursor:
        ctx, record = agent_context(cursor, request, tenant, space, agent)
        cursor.execute("SELECT role FROM hub_tenancy.memberships WHERE tenant_id=%s AND user_id=%s AND active",
                       [tenant, ctx["principal"]])
        role = cursor.fetchone()
        can_notify = bool(role and role[0] != "viewer")
        if request.method == "POST":
            try:
                action = params.get("action")
                if action == "gerar":
                    try:
                        request_id = UUID(params.get("request_id", ""))
                    except ValueError:
                        raise ValueError(tr("Atualize a página antes de solicitar o relatório.")) from None
                    result = avihub_reports.enqueue(tenant, space, agent, user, {**args, "request_id":str(request_id)},
                                                    can_notify=can_notify)
                    if result.get("relatorio_id"):
                        return redirect(f"{destination}?relatorio_id={result['relatorio_id']}#relatorios")
                    error = result.get("mensagem", tr("Confira as opções da conta e da unidade do relatório."))
                elif action != "opcoes":
                    return HttpResponseBadRequest(tr("Ação indisponível."))
            except ValueError as exc:
                error = str(exc)
        try:
            if selected_id:
                selected_id = str(UUID(selected_id))
                selected = avihub_reports.status(tenant, space, agent, user, {"relatorio_id":selected_id})
                reports_data = [visible_report(selected)]
            else:
                reports_data = [visible_report(row) for row in avihub_reports.status(tenant, space, agent, user, {}).get("relatorios", [])]
        except ValueError as exc:
            if selected_id:
                raise Http404 from None
            error = error or str(exc)
            reports_data = []
        if not selected_id:
            try:
                options = avihub_reports.options(tenant, space, agent, user, args, can_notify=can_notify)
            except ValueError as exc:
                error = error or str(exc)
                # Retain the list of personal accounts so a different one can be selected after an error.
                options["contas"] = [account.account_info() for account in
                                      avihub.accounts_for(avihub.personal(tenant, user).filter(ready=True))]
    ctx.update(agent=record, reports=reports_data, options=options, args=args, error=error, request_id=request_id,
               selected_report=selected_id, **form_context(options, args))
    return render(request, "avihub_reports.html", ctx, status=400 if error else 200)


@never_cache
@login_required
@require_GET
def download(request, tenant, space, agent, report_id):
    with transaction.atomic(), connection.cursor() as cursor:
        agent_context(cursor, request, tenant, space, agent)
        try:
            return avihub_reports.download(tenant, space, agent, request.user, report_id)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc), content_type="text/plain; charset=utf-8")
