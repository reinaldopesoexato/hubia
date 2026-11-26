"""Exercise read-only AviHUB notices through the MCP and the portal."""
import json
from datetime import timedelta
from secrets import token_urlsafe
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model
from urllib3 import HTTPHeaderDict

from . import avihub, avihub_client, mcp
from .models import Link


def verify_notices(manager, viewer, user, other, tenant, space, agent, account):
    url = reverse("avihub_notifications", args=[tenant, space, agent.pk])
    term = "Lote QA & Seleção"

    def notice(item_id, title):
        return {"id":item_id, "params":{"title":title, "message":"Peso Aves", "url":"/reports?report_id=501"},
                "created_at":"2026-11-26T08:30:00-03:00", "notification_type":"informational"}

    own = notice(501, term)
    remote = notice(901, "Notificação da outra conta")
    other_panel = avihub.Panel(agent.origin, {"inbox_fixture":"other"})
    foreign = avihub.AviHubSession.objects.create(agent=agent, user=other, ready=True, encrypted=avihub.encrypt(other_panel))

    def backend(target, payload, headers, **options):
        request = urlsplit(target)
        assert request.hostname == "avihub.example" and request.path == "/notifications.json", request.path
        assert payload is None and headers["Accept"] == "application/json"
        query = parse_qs(request.query)
        assert query.keys() <= {"page", "search", "read_status"}, query.keys()
        if "inbox_fixture=other" in headers["Cookie"]:
            rows = [remote]
        elif query.get("search"):
            assert query["search"] == [term] and query.get("read_status") == ["unread"]
            rows = [own] if query["page"] == ["2"] else []
        else:
            rows = [notice(i, f"Notificação {i}") for i in range(1, 21)] if query["page"] == ["1"] else [own]
        return 200, HTTPHeaderDict(), json.dumps(rows).encode()

    def authorization(owner):
        app = get_application_model().objects.create(user=owner, name="Fixture notificações AviHUB", client_type="public",
                                                     authorization_grant_type="authorization-code")
        Link.objects.create(user=owner, tenant=tenant, space=space, protocol="mcp", name=app.name, client_id=app.client_id)
        grant = get_access_token_model().objects.create(user=owner, application=app, scope="hub.read",
                                                        token=token_urlsafe(32), expires=timezone.now() + timedelta(minutes=10))
        return {"HTTP_AUTHORIZATION":"Bearer " + grant.token}

    owner_headers, other_headers = authorization(user), authorization(other)

    def rpc(method, params, headers=owner_headers):
        response = viewer.post("/conta/mcp", {"jsonrpc":"2.0", "id":1, "method":method, "params":params},
                               content_type="application/json", secure=True, **headers)
        assert response.status_code == 200, response.status_code
        return response.json()

    def call(name, arguments, headers=owner_headers):
        return rpc("tools/call", {"name":name, "arguments":arguments}, headers)

    def data(response):
        assert "result" in response, response
        return json.loads(response["result"]["content"][0]["text"])

    general = {"conta":account.account_ref}
    filters = {**general, "pagina":"2", "busca":term, "filtro":"nao_lidas"}
    detail = {**filters, "notificacao_id":"501"}
    try:
        with patch.object(avihub_client, "public_response", side_effect=backend) as request_native:
            advertised = {tool["name"]:tool for tool in rpc("tools/list", {})["result"]["tools"]}
            for name in (mcp.AVIHUB_LIST_NOTICES, mcp.AVIHUB_SEARCH_NOTICES, mcp.AVIHUB_READ_NOTICE):
                assert advertised[name]["annotations"]["readOnlyHint"]
            listing = data(call(mcp.AVIHUB_LIST_NOTICES, general))
            assert listing["conta"]["id"] == account.account_ref and listing["proxima_pagina"] == 2
            next_page = data(call(mcp.AVIHUB_LIST_NOTICES, {**general, "pagina":"2"}))
            assert [row["id"] for row in next_page["notificacoes"]] == ["501"] and next_page["proxima_pagina"] is None
            results = data(call(mcp.AVIHUB_SEARCH_NOTICES, filters))
            assert results["notificacoes"][0]["titulo"] == term and results["pagina"] == 2
            result = data(call(mcp.AVIHUB_READ_NOTICE, detail))
            assert result["notificacao"]["id"] == "501" and not result["notificacao"]["lida"]

            count = request_native.call_count
            assert "error" in call(mcp.AVIHUB_LIST_NOTICES, {"conta":foreign.account_ref})
            assert "error" in call(mcp.AVIHUB_READ_NOTICE, detail, other_headers)
            assert request_native.call_count == count
            inaccessible = {"conta":foreign.account_ref, "notificacao_id":"501"}
            assert data(call(mcp.AVIHUB_READ_NOTICE, inaccessible, other_headers))["estado"] == "nao_encontrada"
            assert manager.get(url, inaccessible, secure=True).status_code == 404
            assert viewer.get(url, {"conta":foreign.account_ref}, secure=True).status_code == 400

            portal = Client(enforce_csrf_checks=True, HTTP_HOST="hubia.psxt.io")
            portal.force_login(user)
            form = portal.get(url, general, secure=True)
            assert form.status_code == 200 and b"Notifica" in form.content
            assert portal.post(url, filters, secure=True).status_code == 403
            security = {"HTTP_ORIGIN":"https://hubia.psxt.io", "HTTP_X_CSRFTOKEN":portal.cookies[settings.CSRF_COOKIE_NAME].value}
            form = portal.post(url, filters, secure=True, **security)
            assert form.status_code == 200 and b"Lote QA &amp; Sele" in form.content
            assert b"busca=Lote" not in form.content
            opened = portal.post(url, detail, secure=True, **security)
            assert opened.status_code == 200 and b"Lote QA &amp; Sele" in opened.content
            assert b"rel=\"noreferrer\"" in opened.content
    finally:
        foreign.delete()
    print("Notificações AviHUB: lista, busca, detalhe, CSRF e isolamento da conta verificados.")
