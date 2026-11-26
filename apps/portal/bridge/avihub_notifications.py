"""Read the linked account's AviHUB inbox within an authorized workspace."""

from functools import partial

from django.db import connection, transaction
from django.http import Http404
from django.urls import reverse

from space_views import active_agent, space_context
from . import avihub, avihub_notification_api as native
from .access import ORIGIN


def authorized_agent(cursor, user, tenant, space, agent_id):
    if not user.is_active:
        raise Http404
    ctx = space_context(cursor, user, tenant, space)
    agent = active_agent(cursor, tenant, space, agent_id)
    if agent["kind"] != "avihub" or not avihub.AviHubAgent.objects.filter(pk=agent_id, tenant=tenant).exists():
        raise Http404
    return ctx, agent


@transaction.atomic
def query(tenant, space, agent_id, user, args):
    with connection.cursor() as cursor:
        authorized_agent(cursor, user, tenant, space, agent_id)
    route = [tenant, space, agent_id]
    link_url = ORIGIN + reverse("use_agent", args=route)
    inbox_url = ORIGIN + reverse("avihub_notifications", args=route)
    session, account_context = avihub.linked_account(tenant, user, agent_id, args.get("conta"), link_url=link_url)
    if session is None:
        return {**account_context, "url_painel":inbox_url}

    filters = {key:args.get(key) for key in ("busca", "pagina", "filtro")}
    if "notificacao_id" in args:
        operation = partial(native.get_notification, notification_id_value=args["notificacao_id"], **filters)
    else:
        operation = partial(native.list_notifications, **filters)
    try:
        result = avihub.use_session(session, operation)
    except avihub.SessionExpired:
        return {**avihub.linking_help(link_url + "?nova=1", expired=True), "url_painel":inbox_url}
    return {**account_context, **result, "url_painel":inbox_url}
