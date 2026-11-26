"""Integrated Peso Aves gateway checks with a simulated native report backend."""
import json
import re
from base64 import b64decode
from datetime import timedelta
from hashlib import sha256
from html import escape
from secrets import token_urlsafe
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model
from urllib3 import HTTPHeaderDict

from . import avihub, avihub_report_api as native, avihub_reports, mcp
from .models import AviHubReport, Link


class NativeReportTransport:
    """Simulate the native HTTP contract while keeping the actual AviHUB client and worker in the test."""

    cookie_name = "_avihub_report_fixture"
    cookie_value = "session-conta-qa"
    csv_content = b"\xff\xfe" + "dia;peso\r\n01/11/2026;1,5\r\n".encode("utf-16le")
    csv_type = "text/csv; charset=utf-16le"
    pdf_content = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    preview_document = '<!doctype html><html lang="pt-BR"><body><h1>Peso Aves</h1><p>Seleção Lote A: 126 aves</p></body></html>'
    rendered_pdf = b"%PDF-1.7\n1 0 obj << /Title (Peso Aves) >> endobj\n%%EOF\n"
    page = """
        <meta charset="utf-8">
        <header><form action="/user_settings/switch_unit"><select name="unit_id">
            <option selected value="1">Matrizes</option>
        </select></form></header>
        <nav class="navbar-body"><form action="/users/sign_out"></form><a href="/reports">Relatórios</a></nav>
    """
    report_form = """
        <form action="/reports" method="post">
            <input type="hidden" name="authenticity_token" value="fixture-authenticity-token">
            <input type="hidden" name="birds" value="true">
            <select name="unit_id[]"><option value="1">Matrizes</option></select>
            <select name="report_type"><option value="csv_aves">CSV Aves</option><option value="pdf">PDF Aves</option></select>
            <select name="nuclei_id[]" id="native_nuclei" multiple></select>
            <select name="batch_id[]" id="native_batches" multiple></select>
            <select name="sector_id[]" id="native_sectors" multiple></select>
            <select name="side_id[]" id="native_sides" multiple></select>
            <select name="side_box_id[]" id="native_boxes" multiple></select>
            <select name="equipment_id[]" id="native_equipment" multiple></select>
            <select name="balance_type"><option value="all">Todas</option><option value="23">PS/SEL</option></select>
            <select name="gender_cd"><option value="F">Fêmeas</option><option value="M">Machos</option></select>
            <select name="visualization_type"><option value="daily">Por dia</option><option value="period">Período</option></select>
        </form>
    """

    def __init__(self, origin):
        self.origin = origin
        self.posts = []
        self.notification_queries = []
        self.confirmed = {}
        self.downloads = []
        self.pdf_binary = True
        self.invalid_file = set()

    @staticmethod
    def event(text, **overrides):
        params = {"tab":"advanced", "birds":"true", "report_type":"pdf", "unit_id":"1", "nuclei_id":"31",
                  "start_date":"01/11/2026", "end_date":"01/11/2026", "batch_id":"81", "equipment_id":"42",
                  "balance_type":"23", "gender_cd":"F", "visualization_type":"daily", **overrides}
        href = escape("/reports?" + urlencode(params, doseq=True), quote=True)
        return f'<a class="group border-l-4 border-l-emerald-500" href="{href}">{text}</a>'

    def response(self, url, data, headers, **options):
        path = urlsplit(url)
        assert path.scheme + "://" + path.netloc == self.origin, path.path
        assert f"{self.cookie_name}={self.cookie_value}" in headers["Cookie"], path.path
        query = parse_qs(path.query)
        response_headers = HTTPHeaderDict()
        if path.path == "/notifications" and data is None:
            return 200, response_headers, self.page.encode()
        if path.path == "/notifications.json" and data is None:
            assert headers["Accept"] == "application/json"
            self.notification_queries.append(query)
            expected_id = query["search"][0].removeprefix("report_id=")
            assert query.get("page") == ["1"], query
            native_url = f"{self.origin}/reports?report_id={expected_id}"
            rows = [
                {"id":701, "params":json.dumps({"url":native_url + "0", "notification_context":"reports_completed"})},
                {"id":702, "params":{"url":f"https://another.example/reports?report_id={expected_id}",
                                    "notification_context":"reports_completed"}},
                {"id":703, "params":{"url":native_url, "notification_context":"reports_failed"}},
            ]
            if expected_id in self.confirmed:
                record = {"url":native_url, "notification_context":"reports_completed"}
                row_id, wrapped = self.confirmed[expected_id]
                rows.append({"id":row_id, "params":json.dumps(record) if wrapped else record})
            return 200, response_headers, json.dumps(rows).encode()
        if path.path == "/reports":
            if data is not None:
                params = parse_qs(data.decode())
                filters = ({"equipment_id[]":["42"]} if not self.posts else
                           {"sector_id[]":["51"], "side_id[]":["61"], "side_box_id[]":["62", "63"],
                            "equipment_id[]":["42", "43"]})
                assert params == {
                    "authenticity_token":["fixture-authenticity-token"], "birds":["true"], "unit_id[]":["1"],
                    "report_type":params["report_type"], "start_date":["2026-11-01"], "end_date":["2026-11-01"],
                    "nuclei_id[]":["31"], "batch_id[]":["81"], "balance_type":["23"], **filters,
                    "gender_cd":["F"], "visualization_type":["daily"],
                }
                assert params["report_type"] in (["csv_aves"], ["pdf"])
                assert headers["Referer"] == self.origin + "/reports?tab=advanced"
                self.posts.append(params)
                report_id = 600 + len(self.posts)
                response_headers["Location"] = f"/reports?tab=advanced&report_id={report_id}"
                return 302, response_headers, b""
            if "report_id" in query:
                assert query["report_id"][0] in self.confirmed
                preview = f'<iframe id="report-iframe" srcdoc="{escape(self.preview_document, quote=True)}"></iframe>'
                return 200, response_headers, (self.page + preview).encode()
            if query.get("tab") == ["basic"]:
                assert query == {"tab":["basic"], "nuclei_id":["31"], "start_date":["2026-11-01"],
                                 "end_date":["2026-11-07"]}, query
                timeline = ('<form action="/reports"><input name="tab" value="basic"></form>'
                            '<select name="nuclei_id"><option value="31" selected>Núcleo A</option></select>'
                            '<p class="text-lg font-bold text-secondary-900">126 aves</p>'
                            + self.event("Seleção Lote A") + self.event("Evento em outra unidade", unit_id="2")
                            + self.event("Seleção com dois equipamentos", sector_id="51", side_id="61",
                                         **{"equipment_id": [], "equipment_id[]":["42", "43"], "side_box_id[]":["62", "63"]}))
                return 200, response_headers, (self.page + timeline).encode()
            assert query == {"tab":["advanced"]}, query
            return 200, response_headers, (self.page + self.report_form).encode()
        if path.path == "/load_children" and data is None:
            assert query["units"] == ["1"]
            selector = query["targets"][0]
            choices = {
                "nuclei_id":[("31", "Núcleo A")], "native_nuclei":[("31", "Núcleo A")], "native_batches":[("81", "Lote A")],
                "native_sectors":[("51", "Galpão A")], "native_sides":[("61", "Lado A")],
                "native_boxes":[("62", "Box A"), ("63", "Box B")], "native_equipment":[("42", "PS/SEL 123"), ("43", "PS/SEL 456")],
            }
            if selector not in ("nuclei_id", "native_nuclei"):
                assert query.get("nucleis") == ["31"], query
            if selector in ("native_sides", "native_boxes"):
                assert query.get("sectors") == ["51"], query
            if selector == "native_boxes":
                assert query.get("sides") == ["61"], query
            if selector == "native_equipment":
                assert query.get("models") == ["23"], query
            options = "".join(f'<option value="{value}">{name}</option>' for value, name in choices[selector])
            body = (f'<turbo-stream action="update" target="{selector}"><template>'
                    f'{options}</template></turbo-stream>')
            return 200, response_headers, body.encode()
        if match := re.fullmatch(r"/reports/([1-9][0-9]*)/download(\.pdf)?", path.path):
            report_id, pdf = match.groups()
            assert data is None and report_id in self.confirmed, path.path
            self.downloads.append(report_id)
            if report_id in self.invalid_file:
                response_headers["Content-Type"] = "text/html"
                return 200, response_headers, b"<html>Sem arquivo</html>"
            if pdf and not self.pdf_binary:
                response_headers["Location"] = f"/reports?tab=advanced&report_id={report_id}"
                return 302, response_headers, b""
            assert headers["Accept"] == ("application/pdf" if pdf else "text/csv")
            mime_type, extension, content = (("application/pdf", "pdf", self.pdf_content) if pdf else
                                             (self.csv_type, "csv", self.csv_content))
            response_headers["Content-Type"] = mime_type
            response_headers["Content-Disposition"] = f'attachment; filename="peso-aves.{extension}"'
            return 200, response_headers, content
        raise AssertionError(path.path)


def verify_native_roundtrip(manager, viewer, agent, account, route, mcp_call, rpc, payload, other_headers):
    transport = NativeReportTransport(agent.origin)
    account.refresh_from_db(fields=["encrypted"])
    previous_session = bytes(account.encrypted)
    account.encrypted = avihub.encrypt(avihub.Panel(agent.origin, {transport.cookie_name:transport.cookie_value}))
    account.save(update_fields=["encrypted"])
    tenant, space, _ = route
    url = reverse("avihub_reports", args=route)
    context = {"conta":account.account_ref, "unidade":"1"}
    period = {"nucleo_id":"31", "inicio":"2026-11-01", "fim":"2026-11-07"}

    def status(report_id):
        return payload(mcp_call(mcp.REPORT_STATUS, {"relatorio_id":report_id}))

    def confirm_due(report_id):
        AviHubReport.objects.filter(pk=report_id).update(next_check_at=timezone.now())
        assert avihub_reports.confirm_next(report_id=report_id)

    try:
        with patch("bridge.avihub_client.public_response", side_effect=transport.response):
            reading = payload(mcp_call("resumir_pesagens", {**context, **period}))["resultado"]
            assert "Evento em outra unidade" in reading["eventos"]
            event, multi = reading["eventos_peso_aves"]
            assert event["tipo"] == "selecao" and event["descricao"] == "Seleção Lote A"
            filters = {**period, "fim":"2026-11-01", "lote_id":"81", "equipamento_id":"42", "tipo_balanca":"23",
                       "sexo":"F", "visualizacao":"daily"}
            assert event["filtros"] == filters
            multi_filters = {key:value for key,value in filters.items() if key != "equipamento_id"}
            multi_filters.update(galpao_id="51", lado_id="61")
            assert multi["filtros"] == multi_filters
            assert multi["filtros_multiplos"] == {"equipamento_id":["42", "43"], "box_id":["62", "63"]}
            selected_url = urlsplit(event["url_portal"])
            assert parse_qs(selected_url.query) == {key:[value] for key,value in {**context, **filters,"formato":"pdf"}.items()}
            prefills = viewer.get(selected_url.path + "?" + selected_url.query, secure=True)
            assert prefills.status_code == 200
            form_data = BeautifulSoup(prefills.content, "html.parser")
            for key, value in (("formato", "pdf"), ("lote_id", "81"), ("equipamento_id", "42"), ("tipo_balanca", "23"),
                               ("sexo", "F"), ("visualizacao", "daily")):
                assert form_data.select_one(f'select[name="{key}"] option[selected]')["value"] == value

            multiple_params = {**context, **multi_filters, "equipamento_id":"42,43", "box_id":"62,63", "formato":"pdf"}
            assert multi["parametros_pdf"] == {"space_id":str(space), **multiple_params}
            selected_url = urlsplit(multi["url_portal"])
            assert parse_qs(selected_url.query) == {key:[value] for key,value in multiple_params.items()}
            prefills = viewer.get(selected_url.path + "?" + selected_url.query, secure=True)
            assert prefills.status_code == 200
            form_data = BeautifulSoup(prefills.content, "html.parser")
            for key, values in multi["filtros_multiplos"].items():
                assert [option["value"] for option in form_data.select(f'select[name="{key}"] option[selected]')] == values
            invalid_selection = {**multiple_params, "equipamento_id":"42,99", "canal":"painel", "request_id":str(uuid4())}
            assert "error" in mcp_call(mcp.REPORT_GENERATE, invalid_selection)
            assert not transport.posts

            params = {**context, **event["filtros"], "formato":"csv_aves", "canal":"painel"}
            options = payload(mcp_call(mcp.REPORT_OPTIONS, {key:value for key,value in params.items() if key != "canal"}))
            assert options["filtros"]["tipo_balanca"][-1] == {"id":"23", "nome":"PS/SEL"}
            assert options["filtros"]["lote_id"] == [{"id":"81", "nome":"Lote A"}]
            assert options["canais"][0]["canal"] == "painel"
            csvid = str(uuid4())
            queued = payload(mcp_call(mcp.REPORT_GENERATE, {**params, "request_id":csvid}))
            report_id = queued["relatorio_id"]
            assert queued["estado"] == "pendente" and "url_download" not in queued
            assert queued == payload(mcp_call(mcp.REPORT_GENERATE, {**params, "request_id":csvid}))
            assert "error" in mcp_call(mcp.REPORT_GENERATE, {**params, "formato":"pdf", "request_id":csvid})
            assert AviHubReport.objects.filter(request_id=csvid, user_id=account.user_id).count() == 1
            assert not transport.posts

            filename_url = reverse("avihub_report_download", args=[*route, report_id])
            report_page = f"{url}?relatorio_id={report_id}"
            assert viewer.get(filename_url, secure=True).status_code == 404
            assert "error" in mcp_call(mcp.REPORT_STATUS, {"relatorio_id":report_id}, other_headers)
            assert avihub_reports.generate_next(report_id=report_id)
            assert len(transport.posts) == 1 and len(transport.notification_queries) == 1
            pending = status(report_id)
            assert pending["estado"] == "aguardando_notificacao" and "notificacao_avihub_id" not in pending
            assert "url_download" not in pending and "arquivo_uri" not in pending and not transport.downloads

            # A real notification for this POST still requires a valid native attachment.
            transport.confirmed["601"] = (711, True)
            transport.invalid_file.add("601")
            confirm_due(report_id)
            assert status(report_id)["estado"] == "aguardando_notificacao" and transport.downloads == ["601"]
            transport.invalid_file.clear()
            confirm_due(report_id)
            assert len(transport.posts) == 1 and transport.downloads == ["601", "601"]

            response = mcp_call(mcp.REPORT_STATUS, {"relatorio_id":report_id})
            available = payload(response)
            assert available["estado"] == "pronto" and available["url_download"].endswith(filename_url)
            assert available["fonte"] == "AviHUB" and available["notificacao_avihub_id"] == "711"
            assert available["arquivo_nome"] == "peso-aves.csv" and available["arquivo_tipo"] == transport.csv_type
            assert available["url_notificacoes_avihub"] == agent.origin + "/notifications"
            listed = payload(mcp_call(mcp.REPORT_STATUS, {}))["relatorios"]
            assert listed == [{key:value for key,value in available.items() if key != "arquivo_uri"}]
            assert manager.get(report_page, secure=True).status_code == 404
            assert manager.get(filename_url, secure=True).status_code == 404
            assert transport.downloads == ["601", "601"]

            # The model-independent MCP client can request the genuine native bytes by resource URI.
            _, file_handle = response["result"]["content"]
            uri = f"hubia://avihub/relatorios/{space}/{report_id}"
            assert file_handle["type"] == "resource_link" and file_handle["uri"] == available["arquivo_uri"] == uri
            assert file_handle["name"] == "peso-aves.csv" and file_handle["mimeType"] == transport.csv_type
            binary = rpc("resources/read", {"uri":uri})["result"]["contents"][0]
            assert binary["uri"] == uri and binary["mimeType"] == transport.csv_type
            assert b64decode(binary["blob"]) == transport.csv_content
            assert "error" in rpc("resources/read", {"uri":uri}, other_headers)

            ready = viewer.get(report_page, secure=True)
            assert ready.status_code == 200 and filename_url.encode() in ready.content
            downloaded = viewer.get(filename_url, secure=True)
            assert downloaded.status_code == 200 and downloaded.content == transport.csv_content
            assert downloaded["Content-Type"] == transport.csv_type
            assert 'filename="peso-aves.csv"' in downloaded["Content-Disposition"]

            # Use upstream PDF when possible and persist rendered bytes from the legacy native preview.
            for native_id, source, expected_bytes in (
                ("602", "AviHUB", transport.pdf_content),
                ("603", "Prévia do AviHUB", transport.rendered_pdf),
                ("604", None, None),
            ):
                transport.pdf_binary = source == "AviHUB"
                transport.confirmed[native_id] = (int(native_id) + 110, False)
                with patch.object(native, "render_pdf", return_value=transport.rendered_pdf,
                                  side_effect=ValueError("Falha simulada de impressão") if expected_bytes is None else None) as render:
                    selection = {**multiple_params, "canal":"painel", "request_id":str(uuid4())}
                    if transport.pdf_binary:
                        pdf_id = payload(mcp_call(mcp.REPORT_GENERATE, selection))["relatorio_id"]
                    else:
                        queued = viewer.post(url, {**selection, **multi["filtros_multiplos"], "action":"gerar"}, secure=True)
                        assert queued.status_code == 302
                        pdf_id = parse_qs(urlsplit(queued.url).query)["relatorio_id"][0]
                    assert avihub_reports.generate_next(report_id=pdf_id)
                    pdf = status(pdf_id)
                    if expected_bytes is None:
                        assert pdf["estado"] == "aguardando_notificacao"
                        assert "url_download" not in pdf and "arquivo_uri" not in pdf
                        expired = timezone.now() - avihub_reports.CONFIRM_TIMEOUT - timedelta(seconds=1)
                        AviHubReport.objects.filter(pk=pdf_id).update(started_at=expired)
                        confirm_due(pdf_id)
                        pdf = status(pdf_id)
                    assert pdf["notificacao_avihub_id"] == str(int(native_id) + 110)
                    pdf_url = reverse("avihub_report_download", args=[tenant, space, agent.pk, pdf_id])
                    pdf_response = viewer.get(pdf_url, secure=True)
                    if expected_bytes:
                        assert pdf["estado"] == "pronto" and pdf["fonte"] == source
                        assert pdf_response.status_code == 200 and pdf_response.content == expected_bytes
                        assert pdf["arquivo_tipo"] == pdf_response["Content-Type"] == "application/pdf"
                        assert pdf["url_download"].endswith(pdf_url) and pdf["arquivo_tamanho"] == len(expected_bytes)
                        assert pdf["arquivo_uri"] == f"hubia://avihub/relatorios/{space}/{pdf_id}"
                        blob = rpc("resources/read", {"uri":pdf["arquivo_uri"]})["result"]["contents"][0]["blob"]
                        assert sha256(b64decode(blob)).digest() == sha256(expected_bytes).digest()
                    else:
                        assert pdf["estado"] == "somente_previa"
                        assert pdf_response.status_code == 302
                        preview_location = urlsplit(pdf_response["Location"])
                        assert f"{preview_location.scheme}://{preview_location.netloc}" == transport.origin
                        assert preview_location.path == "/reports" and parse_qs(preview_location.query)["report_id"] == [native_id]
                        assert pdf["url_previa"].endswith(pdf_url) and "url_download" not in pdf and "arquivo_uri" not in pdf
                    if transport.pdf_binary:
                        render.assert_not_called()
                    elif expected_bytes:
                        render.assert_called_once_with(transport.preview_document)
                    else:
                        assert render.call_count == 2
                        assert all(call.args == (transport.preview_document,) for call in render.call_args_list)
            assert len(transport.posts) == 4
    finally:
        account.encrypted = previous_session
        account.save(update_fields=["encrypted"])


def verify_reports(manager, viewer, user, other, tenant, space, agent, account):
    url = reverse("avihub_reports", args=[tenant, space, agent.pk])
    files = [{"id":"csv_aves", "nome":"CSV Aves", "extensao":"csv", "modo":"arquivo"},
             {"id":"pdf", "nome":"PDF Aves", "extensao":"pdf", "modo":"arquivo"}]
    options = {"estado":"opcoes", "unidade":{"id":"1", "nome":"Matrizes", "ativa":True},
               "formatos":files, "filtros":{"nucleo_id":[{"id":"31", "nome":"Núcleo A"}]}}

    def form(panel, args, *, for_generation=False):
        if not for_generation:
            return options
        return {**options, "parametros":{"unidade":"1", **args}}

    def authorization(owner):
        app = get_application_model().objects.create(user=owner, name="Fixture Peso Aves", client_type="public",
                                                            authorization_grant_type="authorization-code")
        Link.objects.create(user=owner, tenant=tenant, space=space, protocol="mcp", name="Fixture Peso Aves",
                            client_id=app.client_id)
        grant = get_access_token_model().objects.create(user=owner, application=app, scope="hub.read",
                  token=token_urlsafe(32), expires=timezone.now() + timedelta(minutes=10))
        return "Bearer " + grant.token

    headers = {"HTTP_AUTHORIZATION":authorization(user), "HTTP_MCP_PROTOCOL_VERSION":"2025-06-18"}
    other_headers = {"HTTP_AUTHORIZATION":authorization(other), "HTTP_MCP_PROTOCOL_VERSION":"2025-06-18"}

    def rpc(method, params, headers=headers):
        response = viewer.post("/conta/mcp", {"jsonrpc":"2.0", "id":1, "method":method, "params":params},
                               content_type="application/json", secure=True, **headers)
        assert response.status_code == 200, response.status_code
        return response.json()

    def call(name, params, headers=headers):
        return rpc("tools/call", {"name":name,"arguments":params}, headers)

    def payload(result):
        assert "result" in result, result
        return json.loads(result["result"]["content"][0]["text"])

    params = {"conta":account.account_ref, "unidade":"1", "nucleo_id":"31", "formato":"csv_aves",
              "inicio":"2026-11-01", "fim":"2026-11-07", "canal":"painel", "request_id":str(uuid4())}
    backend_data = b"dia;peso\r\n01/11/2026;1,5\r\n"
    with patch.object(native, "prepare", side_effect=form), patch.object(native, "generate") as generate:
        tools = {spec["name"]:spec for spec in rpc("tools/list", {})["result"]["tools"]}
        assert not tools[mcp.REPORT_GENERATE]["annotations"]["readOnlyHint"]
        assert tools[mcp.REPORT_GENERATE]["annotations"]["idempotentHint"]
        assert tools[mcp.REPORT_OPTIONS]["annotations"]["readOnlyHint"]
        assert tools[mcp.REPORT_STATUS]["annotations"]["readOnlyHint"]
        lookup = {key:params[key] for key in ("conta", "unidade", "nucleo_id", "inicio", "fim")}
        scope = {key:lookup[key] for key in ("nucleo_id", "inicio", "fim")}
        for name, details, extra in (
            ("resumir_pesagens", {"total_exibido":"126 aves", "filtros_peso_aves":scope}, {}),
            ("consultar_pesagens", {"registros":[], "filtros_peso_aves":{**scope,"equipamento_id":"42"}},
             {"serie":"fixture", "modelo":"PS/SEL"}),
        ):
            assert tools[name]["annotations"]["readOnlyHint"]
            assert mcp.REPORT_GENERATE in tools[name]["description"]
            expected = {**lookup, "formato":"pdf"}
            if name == "consultar_pesagens":
                expected["equipamento_id"] = "42"
            with patch.object(avihub.api, "unit_query", return_value={"unidade":options["unidade"],"resultado":details}):
                reading = payload(call(name, {**lookup, **extra}))
            handoff = reading["relatorio_peso_aves"]
            assert reading["resultado"] == {key:value for key,value in details.items() if key != "filtros_peso_aves"}
            assert handoff["parametros_pdf"] == {"space_id":str(space), **expected}
            link = urlsplit(handoff["url_portal"])
            assert link.path == url and link.netloc == "hubia.psxt.io"
            assert parse_qs(link.query) == {key:[value] for key,value in expected.items()}
            assert mcp.REPORT_STATUS in handoff["instrucao_para_ia"]
            assert not AviHubReport.objects.filter(user=user).exists()
        generate.assert_not_called()
        available = payload(call(mcp.REPORT_OPTIONS, {"conta":account.account_ref, "unidade":"1"}))
        assert available["formatos"] == files and available["canais"][0]["canal"] == "painel"
        form_page = viewer.get(url, secure=True)
        assert form_page.status_code == 200 and b'name="formato"' in form_page.content
        assert b'CSV Aves' in form_page.content and b'PDF Aves' in form_page.content

    verify_native_roundtrip(manager, viewer, agent, account, [tenant, space, agent.pk], call, rpc, payload, other_headers)

    with patch.object(native, "prepare", side_effect=form), patch.object(native, "generate", side_effect=native.SubmissionUncertain) as generate:
        uncertain_request = {**params, "request_id":str(uuid4())}
        queued = payload(call(mcp.REPORT_GENERATE, uncertain_request))
        assert avihub_reports.generate_next(report_id=queued["relatorio_id"])
        assert not avihub_reports.generate_next(report_id=queued["relatorio_id"])
        generate.assert_called_once()
        uncertain = payload(call(mcp.REPORT_GENERATE, uncertain_request))
        assert uncertain["estado"] == "incerto" and uncertain["relatorio_id"] == queued["relatorio_id"]
        assert "url_download" not in uncertain and uncertain["url_notificacoes_avihub"].endswith("/notifications")

    with patch.object(native, "prepare", side_effect=form), patch.object(native, "generate", return_value="503"), \
            patch.object(native, "find_notification", return_value="803"), \
            patch.object(avihub_reports.delivery, "validate", return_value="cliente@example.invalid"), \
            patch.object(native, "download", return_value=("peso.csv", "text/csv", backend_data)), \
            patch.object(avihub_reports.delivery, "deliver", return_value="accepted") as deliver:
        mail_request = {**params, "request_id":str(uuid4()), "canal":"email_anexo"}
        mail_report = payload(call(mcp.REPORT_GENERATE, mail_request))
        assert avihub_reports.generate_next(report_id=mail_report["relatorio_id"])
        assert payload(call(mcp.REPORT_STATUS, {"relatorio_id":mail_report["relatorio_id"]}))["entrega"] == "pending"
        assert avihub_reports.deliver_next(report_id=mail_report["relatorio_id"])
        assert not avihub_reports.deliver_next(report_id=mail_report["relatorio_id"])
        assert deliver.call_args.kwargs["attachment"] == ("peso.csv", "text/csv", backend_data)
        assert payload(call(mcp.REPORT_STATUS, {"relatorio_id":mail_report["relatorio_id"]}))["entrega"] == "accepted"

    with patch.object(native, "prepare", side_effect=form), patch.object(native, "generate") as generate:
        revoked = payload(call(mcp.REPORT_GENERATE, {**params, "request_id":str(uuid4())}))
        Link.objects.get(user=user, name="Fixture Peso Aves").delete()
        assert avihub_reports.generate_next(report_id=revoked["relatorio_id"])
        generate.assert_not_called()
        assert AviHubReport.objects.get(pk=revoked["relatorio_id"]).state == AviHubReport.State.FAILED
    print("Peso Aves: seleção, notificação da sessão, arquivo nativo, impressão da prévia e entrega verificados com HTTP simulado.")

    from .smoke_avihub_report_delivery import verify_delivery
    verify_delivery()
