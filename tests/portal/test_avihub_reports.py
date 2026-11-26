import json
import sys
import unittest
from base64 import b64decode, b64encode
from html import escape
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from urllib3 import HTTPHeaderDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge import avihub_report_api as api
from bridge.avihub_client import Panel


def layout(body="", *, active="1", multiple=False):
    ids = ("1", "2") if multiple else ("1",)
    choices = "".join(f'<option value="{value}" {"selected" if value == active else ""}>Unidade {value}</option>'
                      for value in ids)
    return ('<header><form action="/user_settings/switch_unit"><select name="unit_id">' + choices +
            '</select></form></header><nav class="navbar-body"><form action="/users/sign_out"></form>'
            '<a href="/reports">Relatórios</a></nav>' + body)


def turbo(target, value, label):
    return turbo_choices(target, [(value, label)])


def turbo_choices(target, choices):
    options = "".join(f'<option value="{value}">{label}</option>' for value, label in choices)
    return f'<turbo-stream action="update" target="{target}"><template>{options}</template></turbo-stream>'


FORM = '''
<form action="/reports" method="post" data-controller="report-form">
  <input type="hidden" name="authenticity_token" value="csrf-fixture">
  <input type="hidden" name="birds" value="true">
  <select name="unit_id[]"><option value="1">Unidade avícola</option></select>
  <select name="report_type">
    <option value="excel_aves">Excel detalhado</option>
    <option value="pdf_summary">PDF resumido</option>
    <option value="csv_aves_multiblocos">CSV multiblocos</option>
    <option value="txt_aves" disabled>TXT indisponível</option>
    <option value="formato_nao_suportado">Outro</option>
  </select>
  <select name="nuclei_id[]" id="nuclei_id" multiple></select>
  <select name="batch_id[]" id="batch_id" multiple></select>
  <select name="sector_id[]" id="sector_id" multiple></select>
  <select name="side_id[]" id="side_id" multiple></select>
  <select name="side_box_id[]" id="side_box_id" multiple></select>
  <select name="equipment_id[]" id="equipment_id" multiple></select>
  <select name="gender_cd"><option value="1">Fêmea</option></select>
  <select name="balance_type"><option value="all">Todos</option></select>
  <select name="weighing_schedule"><option value="interval">Dentro do intervalo</option></select>
  <select name="visualization_type"><option value="daily">Por dia</option></select>
</form>
'''


class NativePesoAvesTests(unittest.TestCase):
    args = {"inicio": "2026-11-01", "fim": "2026-11-25", "formato": "excel_aves"}

    def panel(self, *get_pages, exchange=None):
        panel = Panel("https://panel.example")
        panel.request = Mock(side_effect=[(200, None, page.encode()) for page in get_pages])
        panel.exchange = Mock(return_value=exchange)
        return panel

    def test_reads_natively_available_formats_and_choices(self):
        panel = self.panel(layout(), layout(FORM), turbo("nuclei_id", "7", "Núcleo QA"),
                           turbo("equipment_id", "17", "PSSEL/123"))
        data = api.prepare(panel, {})
        self.assertEqual(data["estado"], "opcoes")
        self.assertEqual(data["unidade"]["id"], "1")
        self.assertEqual(data["filtros"]["nucleo_id"], [{"id": "7", "nome": "Núcleo QA"}])
        self.assertEqual(data["filtros"]["equipamento_id"], [{"id": "17", "nome": "PSSEL/123"}])
        self.assertEqual({item["id"]: item["modo"] for item in data["formatos"]},
                         {"excel_aves": "arquivo", "pdf_summary": "arquivo", "csv_aves_multiblocos": "arquivo"})
        panel.exchange.assert_not_called()
        self.assertNotIn("csrf-fixture", str(data))

    def test_validates_parameters_and_submits_exact_native_form(self):
        pages = (layout(), layout(FORM), turbo("nuclei_id", "7", "Núcleo QA"), turbo("batch_id", "8", "Lote QA"))
        args = dict(self.args, formato="xlsx", nucleo_id="7", lote_id="8", sexo="1")
        prepared = api.prepare(self.panel(*pages), args, for_generation=True)
        self.assertEqual(prepared["parametros"], dict(args, formato="excel_aves", unidade="1"))
        panel = self.panel(*pages, exchange=(302, "/reports?report_id=25&tab=advanced", {}, b""))
        self.assertEqual(api.generate(panel, args), "25")
        sent = {"authenticity_token": "csrf-fixture", "birds": "true", "unit_id[]": "1", "report_type": "excel_aves",
                "start_date": "2026-11-01", "end_date": "2026-11-25", "nuclei_id[]": "7", "batch_id[]": "8", "gender_cd": "1"}
        panel.exchange.assert_called_once_with("/reports", sent, referer="/reports?tab=advanced", read_timeout=90)
        option_request = urlsplit(panel.request.call_args.args[0])
        self.assertEqual(parse_qs(option_request.query), {"units": ["1"], "nucleis": ["7"], "targets": ["batch_id"]})

    def test_validates_every_selected_box_and_equipment_in_native_scope(self):
        args = dict(self.args, nucleo_id="7", galpao_id="91", lado_id="52, 51",
                    box_id="92, 81", equipamento_id="22, 17", tipo_balanca="all")
        pages = (layout(), layout(FORM), turbo("nuclei_id", "7", "Núcleo"),
                 turbo("sector_id", "91", "Galpão"), turbo_choices("side_id", [("51", "A"), ("52", "B")]),
                 turbo_choices("side_box_id", [("81", "A / Box 1"), ("92", "B / Box 2")]))
        equipments = turbo_choices("equipment_id", [("17", "Equipamento 17"), ("22", "Equipamento 22")])
        panel = self.panel(*pages, equipments)
        prepared, fields = api._prepare(panel, args, for_generation=True)
        self.assertEqual(prepared["parametros"]["lado_id"], "51,52")
        self.assertEqual(prepared["parametros"]["box_id"], "81,92")
        self.assertEqual(prepared["parametros"]["equipamento_id"], "17,22")
        self.assertEqual(fields["side_id[]"], ["51", "52"])
        self.assertEqual(fields["side_box_id[]"], ["81", "92"])
        self.assertEqual(fields["equipment_id[]"], ["17", "22"])
        requests = [parse_qs(urlsplit(call.args[0]).query) for call in panel.request.call_args_list[-2:]]
        self.assertEqual(requests, [{"units": ["1"], "nucleis": ["7"], "sectors": ["91"],
                                     "sides": ["51,52"], "targets": ["side_box_id"]},
                                    {"units": ["1"], "nucleis": ["7"], "models": ["all"],
                                     "targets": ["equipment_id"]}])

        panel = self.panel(*pages, turbo("equipment_id", "17", "Equipamento 17"))
        with self.assertRaises(api.AccessDenied):
            api.generate(panel, args)
        panel.exchange.assert_not_called()

    def test_rejects_invalid_ids_and_native_single_select(self):
        for field, value in (("nucleo_id", "7,8"), ("box_id", "7,"), ("box_id", "7,07"),
                             ("equipamento_id", "7, outside"), ("equipamento_id", "7,7")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                api.requested_filters({field: value})
        single_select = FORM.replace('id="equipment_id" multiple', 'id="equipment_id"')
        panel = self.panel(layout(), layout(single_select))
        with self.assertRaises(api.AccessDenied):
            api.generate(panel, dict(self.args, equipamento_id="17,22"))
        panel.exchange.assert_not_called()

    def test_requires_explicit_active_unit_and_authorized_filters(self):
        multiple = layout(multiple=True)
        state = api.prepare(self.panel(multiple), self.args)
        self.assertEqual(state["estado"], "escolher_unidade")
        for pages, options in (
            ((multiple,), {"unidade": "2"}),
            ((layout(), layout(FORM.replace('value="1">Unidade avícola', 'value="2">Unidade avícola'))), {}),
            ((layout(), layout(FORM), turbo("nuclei_id", "7", "Núcleo QA")), {"nucleo_id": "8"}),
            ((layout(), layout(FORM)), {"lote_id": "8"}),
            ((layout(), layout(FORM)), {"formato": "txt_aves"}),
            ((layout(), layout(FORM)), {"inicio": "2026-12-01"}),
        ):
            panel = self.panel(*pages)
            with self.subTest(options=options), self.assertRaises(ValueError):
                api.generate(panel, dict(self.args, **options))
            panel.exchange.assert_not_called()

    def test_native_redirect_or_timeout_never_confirms_an_unknown_report(self):
        for reply in ((302, "https://another.example/reports?report_id=25&tab=advanced"),
                      (302, "/reports?report_id=25&report_id=26&tab=advanced"),
                      (302, "/reports?tab=advanced"), (500, None), (200, "/reports")):
            panel = self.panel(layout(), layout(FORM), exchange=(*reply, {}, b""))
            with self.subTest(reply=reply), self.assertRaises(api.SubmissionUncertain):
                api.generate(panel, self.args)
            self.assertEqual(panel.exchange.call_count, 1)
        panel = self.panel(layout(), layout(FORM))
        panel.exchange.side_effect = ValueError("Timeout")
        with self.assertRaises(api.SubmissionUncertain):
            api.generate(panel, self.args)
        self.assertEqual(panel.exchange.call_count, 1)

    def test_native_csrf_prioritizes_form_and_accepts_rails_meta_fallback(self):
        head = ('<head><meta name="csrf-param" content="authenticity_token">'
                '<meta name="csrf-token" content="csrf-meta"></head>')
        without_field = FORM.replace('name="authenticity_token"', 'name="another_field"')
        for form, token in ((FORM, "csrf-fixture"), (without_field, "csrf-meta")):
            reply = (302, "/reports?report_id=25&tab=advanced", {}, b"")
            panel = self.panel(layout(), head + layout(form), exchange=reply)
            self.assertEqual(api.generate(panel, self.args), "25")
            self.assertEqual(panel.exchange.call_args.args[1]["authenticity_token"], token)
        panel = self.panel(layout(), layout(without_field))
        with self.assertRaises(api.AccessDenied):
            api.generate(panel, self.args)
        panel.exchange.assert_not_called()

    def test_native_attachment_preserves_charset_and_bytes_and_checks_mime(self):
        headers = {"Content-Type": "text/csv; charset=utf-16le",
                   "Content-Disposition": "attachment; filename*=UTF-8''Pesagem%20Aves.csv"}
        artifact = b"\xff\xfe" + "Peso\r\n1250".encode("utf-16le")
        panel = self.panel(layout(), exchange=(200, None, headers, artifact))
        self.assertEqual(api.download(panel, "1", "25", "csv_aves_multiblocos"),
                         ("Pesagem Aves.csv", headers["Content-Type"], artifact))
        panel.exchange.assert_called_once_with("/reports/25/download", accept="text/csv", read_timeout=30, limit=api.FILE_LIMIT)
        with self.assertRaises(api.AccessDenied):
            api.download(self.panel(layout(), exchange=(200, None, {"Content-Type": "text/html"}, b"login")),
                         "1", "25", "csv_aves")

    def test_pdf_uses_authorized_native_preview(self):
        page = layout('<iframe id="report-iframe" srcdoc="Peso Aves"></iframe>')
        self.assertEqual(api.preview_url(self.panel(page), "1", "25"),
                         "https://panel.example/reports?report_id=25&tab=advanced")
        with self.assertRaises(api.AccessDenied):
            api.preview_url(self.panel(layout()), "1", "25")
        redirect = (302, "/reports?report_id=25&tab=advanced", {}, b"")
        with self.assertRaises(api.NativePdfUnavailable):
            api.download(self.panel(layout(), exchange=redirect), "1", "25", "pdf")

    def test_pdf_requires_native_binary_and_attachment_type(self):
        attachment = {"Content-Type":"application/pdf", "Content-Disposition":'attachment; filename="Peso Aves.pdf"'}
        artifact = b"%PDF-1.4\n%%EOF\n"
        panel = self.panel(layout(), exchange=(200, None, attachment, artifact))
        self.assertEqual(api.download(panel, "1", "25", "pdf"), ("Peso Aves.pdf", "application/pdf", artifact))
        panel.exchange.assert_called_once_with("/reports/25/download.pdf", accept="application/pdf",
                                               read_timeout=30, limit=api.FILE_LIMIT)
        with self.assertRaises(api.AccessDenied):
            api.download(self.panel(layout(), exchange=(200, None, attachment, b"<html>preview</html>")), "1", "25", "pdf")

    def test_pdf_print_uses_native_preview_and_embeds_verified_assets(self):
        asset = "/assets/logo-a1b2.png"
        png = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4x8AAAAASUVORK5CYII=")
        html = f'<h1>Peso Aves</h1><p>Lote A</p><img src="{asset}"><img src="https://panel.example{asset}">'
        page = layout(f'<iframe id="report-iframe" srcdoc="{escape(html, quote=True)}"></iframe>')
        panel = self.panel(page, exchange=(200, None, {"Content-Type":"image/png"}, png))
        artifact = b"%PDF-1.7\n%%EOF\n"
        with patch.object(api, "render_pdf", return_value=artifact) as render:
            self.assertEqual(api.print_preview(panel, "1", "25"), ("peso-aves-25.pdf", "application/pdf", artifact))
        panel.request.assert_called_once_with("/reports?report_id=25&tab=advanced", accept="text/html")
        panel.exchange.assert_called_once_with(asset, accept="image/png", limit=2 * 1024 * 1024)
        embedded = "data:image/png;base64," + b64encode(png).decode()
        printed_html = render.call_args.args[0]
        self.assertIn("<p>Lote A</p>", printed_html)
        self.assertEqual(printed_html.count(embedded), 2)
        self.assertNotIn(asset, printed_html)

    def test_pdf_print_refuses_unverified_preview_assets(self):
        for source in ("https://another.example/assets/logo.png", "/users/sign_out", "/assets/logo.png?ignored=1"):
            panel = self.panel()
            with self.subTest(source=source), self.assertRaises(ValueError):
                api.preview_images(panel, f'<img src="{source}">')
            panel.exchange.assert_not_called()
        panel = self.panel(exchange=(200, None, {"Content-Type":"image/png"}, b"<html>not a PNG</html>"))
        with self.assertRaises(ValueError):
            api.preview_images(panel, '<img src="/assets/logo.png">')

    def test_notification_requires_own_native_id_and_completed_context(self):
        wrong = [
            {"id":1, "params":{"url":"/reports?report_id=250", "notification_context":"reports_completed"}},
            {"id":2, "params":{"url":"/reports?report_id=25", "notification_context":"reports_failed"}},
            {"id":3, "params":{"url":"https://other.example/reports?report_id=25", "notification_context":"reports_completed"}},
            {"id":4, "params":{"url":"/reports?report_id=25&report_id=26", "notification_context":"reports_completed"}},
        ]
        right = {"id":5, "params":json.dumps({"url":"https://panel.example/reports?report_id=25",
                                               "notification_context":"reports_completed"})}
        panel = self.panel(layout(), json.dumps(wrong * 5), json.dumps([right]))
        self.assertEqual(api.find_notification(panel, "1", "25"), "5")
        request_paths = [call.args[0] for call in panel.request.call_args_list]
        self.assertEqual(request_paths, ["/notifications", "/notifications.json?search=report_id%3D25&page=1",
                                         "/notifications.json?search=report_id%3D25&page=2"])
        self.assertIsNone(api.find_notification(self.panel(layout(), json.dumps(wrong)), "1", "25"))

    def test_exchange_retains_same_origin_query_and_encodes_rails_arrays(self):
        panel = Panel("https://panel.example")
        response = HTTPHeaderDict({"Location": "https://panel.example/reports?report_id=25&tab=advanced"})
        with patch("bridge.avihub_client.public_response", return_value=(302, response, b"")) as request:
            self.assertEqual(panel.exchange("/reports", {"unit_id[]": ["1", "2"]}, read_timeout=90)[1],
                             "/reports?report_id=25&tab=advanced")
            self.assertEqual(parse_qs(request.call_args.args[1].decode()), {"unit_id[]": ["1", "2"]})
            self.assertEqual(request.call_args.kwargs["read_timeout"], 90)
        response["Location"] = "//another.example/reports?report_id=25"
        with patch("bridge.avihub_client.public_response", return_value=(302, response, b"")):
            self.assertIsNone(panel.exchange("/reports")[1])
