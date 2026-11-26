import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge import avihub_notification_api as api
from bridge.avihub_api import AccessDenied, SessionExpired


def history_item(item_id, **overrides):
    return {
        "id": item_id,
        "notification_type": 2,
        "recipient_id": 128,
        "recipient_type": "User",
        "created_at": "2026-11-26T03:05:00.000Z",
        "read_at": "2026-11-26T03:06:00.000Z",
        "favorited": True,
        "params": json.dumps({"title": "Relatório", "message": "Concluído"}),
        **overrides,
    }


class NativeNotificationTests(unittest.TestCase):
    def panel(self, *pages):
        panel = Mock(origin="https://panel.example")
        panel.content.side_effect = [json.dumps(rows).encode() for rows in pages]
        return panel

    def requests(self, panel):
        result = []
        for request in panel.content.call_args_list:
            path, accept = request.args
            url = urlsplit(path)
            self.assertEqual(url.path, "/notifications.json")
            self.assertEqual(accept, "application/json")
            result.append(parse_qs(url.query))
        return result

    def test_projects_safe_fields_and_reuses_native_search(self):
        params = {
            "title": "<b>Peso Aves</b> <style>regra CSS</style>",
            "message": '<script>alert(1)</script> Lote\u202e 21 [Abrir](https://another.example/falso) '
                       'https://another.example/externo &amp; 14 aves',
            "notification_context": "reports_completed",
            "url": "/reports?tab=advanced&report_id=603",
            "media_url": "https://another.example/privado",
            "recipient": "person@example.org",
        }
        panel = self.panel([history_item(32, params=json.dumps(json.dumps(json.dumps(params))))])
        result = api.list_notifications(panel, busca=" lote & aves ", pagina="2", filtro="favoritas")
        self.assertEqual(self.requests(panel), [{"page": ["2"], "search": ["lote & aves"], "favorited": ["true"]}])
        self.assertEqual(result["origem"], "AviHUB")
        self.assertEqual(result["pagina"], 2)
        self.assertEqual(result["notificacoes"], [{
            "id": "32", "titulo": "Peso Aves", "mensagem": "Lote 21 Abrir & 14 aves",
            "tipo": "informational", "lida": True, "favorita": True, "criada_em": "2026-11-26T03:05:00+00:00",
            "url": "https://panel.example/reports?report_id=603&tab=advanced",
        }])
        self.assertFalse(result["pode_haver_mais"])
        self.assertIsNone(result["proxima_pagina"])

    def test_suppresses_external_and_non_read_routes(self):
        for href in (
            "//other.example/reports?report_id=603",
            "https://panel.example@other.example/reports?report_id=603",
            "https://other.example/reports?report_id=603",
            "/reports?report_id=603&auto_generate=1",
            "/reports?tab=advanced&auto_generate=1",
            "/reports?report_id=603&report_id=604",
            "/reports/603/download",
            "/notifications/603/mark_as_read",
            "/user_settings/switch_unit?unit_id=603",
            "javascript:alert(1)",
        ):
            notification = history_item(32, params={"url": href})
            with self.subTest(href=href):
                result = api.list_notifications(self.panel([notification]))
                self.assertNotIn("url", result["notificacoes"][0])

    def test_detail_uses_the_requested_filtered_page_only(self):
        panel = self.panel([history_item(32), history_item(37)])
        result = api.get_notification(panel, "37", pagina="27", busca="PDF", filtro="lidas")
        self.assertEqual(result["notificacao"]["id"], "37")
        self.assertEqual(result["pagina"], 27)
        self.assertEqual(self.requests(panel), [{"page": ["27"], "search": ["PDF"], "read_status": ["read"]}])

    def test_checked_page_window_and_possible_next_page(self):
        items = [history_item(item_id) for item_id in range(100, 120)]
        panel = self.panel(items)
        listing = api.list_notifications(panel, filtro="nao_lidas")
        self.assertEqual(listing["proxima_pagina"], 2)
        self.assertTrue(listing["pode_haver_mais"])
        self.assertEqual(self.requests(panel), [{"page": ["1"], "read_status": ["unread"]}])

        panel = self.panel(items, items, items, items, items)
        missing = api.get_notification(panel, "120")
        self.assertEqual(missing["estado"], "nao_encontrada")
        self.assertEqual(missing["janela_paginas"], {"inicio": 1, "fim": 5})
        self.assertEqual([query["page"] for query in self.requests(panel)], [["1"], ["2"], ["3"], ["4"], ["5"]])

        panel = self.panel(items, [history_item(120)])
        found = api.get_notification(panel, "120")
        self.assertEqual(found["pagina"], 2)
        self.assertEqual(found["notificacao"]["id"], "120")

    def test_invalid_filters_and_provider_responses(self):
        panel = self.panel()
        for options in ({"pagina": "-1"}, {"pagina": "501"}, {"filtro": "externo"}, {"busca": "a" * 121}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                api.list_notifications(panel, **options)
        with self.assertRaises(ValueError):
            api.get_notification(panel, "uuid-a")
        panel.content.assert_not_called()
        for page in ({"notificacoes": []}, [history_item(32)] * 21):
            with self.subTest(page_type=type(page).__name__), self.assertRaises(AccessDenied):
                api.list_notifications(self.panel(page))
        panel.content.side_effect = SessionExpired("vincule novamente")
        with self.assertRaises(SessionExpired):
            api.list_notifications(panel)

    def test_malformed_markup_cannot_hide_the_rest_of_a_page(self):
        malformed = history_item(31, params={"title": "<![foo[abc]]>", "message": "Normal"})
        result = api.list_notifications(self.panel([malformed, history_item(32)]))
        self.assertEqual([item["titulo"] for item in result["notificacoes"]], ["Notificação", "Relatório"])
