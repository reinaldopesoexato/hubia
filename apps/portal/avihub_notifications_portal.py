"""Read the notifications of a linked AviHUB account from its agent page."""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as tr
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from bridge import avihub_notifications


def address(url, filters, **updates):
    query = urlencode({key: value for key, value in {**filters, **updates}.items() if value})
    return f"{url}?{query}" if query else url


def display(notification):
    labels = {"critical":tr("Crítica"), "warning":tr("Alerta"),
              "informational":tr("Informativa"), "automatic":tr("Automática")}
    stamp = notification.get("criada_em")
    return {**notification, "data":parse_datetime(stamp) if stamp else None,
            "tipo_label":labels.get(notification.get("tipo"), "")}


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("busca")
def notifications(request, tenant, space, agent):
    link_fields = ("conta", "pagina", "notificacao_id")
    args = {field: value for field in link_fields
            if (value := request.POST.get(field, request.GET.get(field, "")).strip())}
    args.update({field: value for field in ("busca", "filtro") if (value := request.POST.get(field, "").strip())})
    list_url = reverse("avihub_notifications", args=[tenant, space, agent])
    detail = args.get("notificacao_id")
    result, error = {}, ""
    with transaction.atomic(), connection.cursor() as cursor:
        ctx, record = avihub_notifications.authorized_agent(cursor, request.user, tenant, space, agent)
        try:
            result = avihub_notifications.query(tenant, space, agent, request.user, args)
        except ValueError as exc:
            error = str(exc)

    selected = result.get("conta") or {}
    filters = {"conta":selected.get("id", args.get("conta"))}
    page = result.get("pagina") or 1
    next_page = result.get("proxima_pagina")
    notifications_data = [
        {**display(item), "url_detalhe":address(list_url, filters, pagina=page, notificacao_id=item["id"])}
        for item in result.get("notificacoes", ())
    ]
    item = result.get("notificacao")
    status = 400 if error else 404 if result.get("estado") == "nao_encontrada" else 200
    filtered = args.get("busca") or args.get("filtro")
    ctx.update(agent=record, args=args, result=result, error=error, detail=detail,
               selected_account=filters["conta"], notification=display(item) if item else None, reset_url=list_url,
               language_next_url=address(list_url, filters, pagina=None if filtered or page == 1 else page,
                                         notificacao_id=item["id"] if item and not filtered else None),
               notifications=notifications_data, list_url=address(list_url, filters, pagina=page if page > 1 else None),
               previous_url=address(list_url, filters, pagina=page - 1) if page > 1 else "",
               next_url=address(list_url, filters, pagina=next_page) if next_page else "")
    return render(request, "avihub_notifications.html", ctx, status=status)
