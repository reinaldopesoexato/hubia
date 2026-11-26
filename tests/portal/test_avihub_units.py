import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/portal"))
from bridge import avihub_api as api
from bridge.avihub_client import Panel


def layout(body="", active="1", multiple=False, module="/equipment"):
    options = [("1", "Matrizes"), ("2", "Recria")] if multiple else [("1", "Matrizes")]
    choices = "".join(f'<option value="{i}" {"selected" if i == active else ""}>{n}</option>' for i, n in options)
    return (f'<header><form action="/user_settings/switch_unit"><select name="unit_id">{choices}</select></form></header>'
            f'<nav class="navbar-body"><form action="/users/sign_out"></form><a href="{module}">Módulo</a></nav>{body}')


def card(serial="3434-2023", model="SAC", route="/weights/sac", href=None):
    link = href or route + "?equipment_id=15"
    return (f'<div class="equipment-card" data-equipment-id="15" data-equipment-serie="{serial}" '
            f'data-equipment-model="{model}" data-equipment-version="1.2"><div class="equipment-location">Galpão 2</div>'
            f'<turbo-frame id="equipment_15_last_connection_frame"><div class="text-yellow-500"></div>2 dias</turbo-frame>'
            f'<a href="{link}">Relatório</a></div>')


def grid(content, more=False, unit="1"):
    return (f'<div data-equipment-realtime-unit-id-value="{unit}">{content}</div>' +
            ('<div class="list-pagination"><a rel="next" href="?page=2">Próxima</a></div>' if more else ''))


def table(cells):
    header = ''.join(f'<div>Coluna {n}</div>' for n in range(len(cells)))
    row = ''.join(f'<div>{c}</div>' for c in cells)
    return (f'<div class="list-component"><div class="list-component-header">{header}</div>'
            f'<div class="list-component-row"><div class="list-component-row-link">{row}</div></div></div>')


def turbo(target, options):
    items = ''.join(f'<option value="{key}">{name}</option>' for key, name in options.items())
    return f'<turbo-stream action="update" target="{target}"><template>{items}</template></turbo-stream>'


class NativeQueries(unittest.TestCase):
    lookup = {"serie": "3434-2023", "modelo": "SAC"}
    period = {"inicio": "2026-11-01", "fim": "2026-11-05"}

    def panel(self, *responses):
        panel = Panel("https://panel.example")
        panel.request = Mock(side_effect=[(200, "", body.encode()) if isinstance(body, str) else body for body in responses])
        return panel

    def test_selection_before_equipment(self):
        panel = self.panel(layout(multiple=True))
        prompt = api.unit_query(panel, "consultar_equipamento", self.lookup)
        self.assertEqual(prompt["estado"], "escolher_unidade")
        self.assertEqual(len(prompt["unidades"]), 2)
        self.assertEqual(panel.request.call_count, 1)

    def test_explicit_native_switch_and_recheck(self):
        panel = self.panel(layout(multiple=True), (302, "/notifications", b""), layout(active="2", multiple=True))
        selected = api.unit_query(panel, api.SELECT_UNIT, {"unidade": "Recria"})
        self.assertTrue(selected["unidade"]["ativa"])
        self.assertEqual(panel.request.call_args_list[1].args, ("/user_settings/switch_unit?unit_id=2",))
        denied = self.panel(layout(multiple=True), (302, "/notifications", b""), layout(multiple=True))
        with self.assertRaises(api.AccessDenied):
            api.unit_query(denied, api.SELECT_UNIT, {"unidade": "2"})
        unknown = self.panel(layout())
        with self.assertRaises(api.AccessDenied):
            api.unit_query(unknown, api.SELECT_UNIT, {"unidade": "2"})
        self.assertEqual(unknown.request.call_count, 1)

    def test_wrong_unit_permission_and_revocation(self):
        for body, choice in [(layout(multiple=True), "Recria"), (layout(), "2"), (layout(module="/nucleis"), "1")]:
            panel = self.panel(body)
            with self.assertRaises(api.AccessDenied):
                api.unit_query(panel, "consultar_equipamento", dict(self.lookup, unidade=choice))
            self.assertEqual(panel.request.call_count, 1)
        panel = self.panel(layout(), (302, "/users/sign_in", b""))
        with self.assertRaises(api.SessionExpired):
            api.unit_query(panel, "consultar_equipamento", self.lookup)

    def test_exact_serial_all_search_pages(self):
        panel = self.panel(layout(), layout(grid(card("3434"), True)), layout(grid(card())))
        result = api.unit_query(panel, "consultar_equipamento", self.lookup)
        self.assertEqual((result["id"], result["versao"], result["localizacao"]), ("15", "1.2", "Galpão 2"))
        url = urlsplit(panel.request.call_args.args[0])
        self.assertEqual((url.path, parse_qs(url.query)), ("/equipment", {"page": ["2"], "search": ["3434-2023"]}))
        for batch in [card("3434"), card() + card(), card(model="PE40")]:
            with self.assertRaises(ValueError):
                api.unit_query(self.panel(layout(), layout(grid(batch))), "consultar_equipamento", self.lookup)

    def test_unit_changed_between_responses(self):
        with self.assertRaises(api.AccessDenied):
            api.unit_query(self.panel(layout(), layout(grid(card()), multiple=True, active="2")),
                           "consultar_equipamento", self.lookup)

    def test_native_card_histories(self):
        for op, model, route in [("consultar_sensores", "PE40", "/weights/pe40"),
                                 ("consultar_racao", "SP501S", "/weights/sp501s"),
                                 ("consultar_silos", "SPSILO", "/weights/spsilo"),
                                 ("consultar_historico_sac", "SAC", "/weights/sac"),
                                 ("consultar_pesagens", "PS/SEL", "/weights"),
                                 ("consultar_pesagens", "PS/SEL", "/weights/incubation")]:
            with self.subTest(op=op, route=route):
                cells = [model + "/3434-2023", "valor"]
                if model == "SAC":
                    cells = ["25/11", "3434-2023", "10", "1", "abertura", "2", "3", "1"]
                form = f'<form action="{route}"><input name="equipment_id" value="15"></form>'
                scopes = [turbo("nuclei_id", {"7": "Recria"})] if route == "/weights" else []
                panel = self.panel(layout(), layout(grid(card(model=model, route=route))), *scopes, layout(form + table(cells)))
                result = api.unit_query(panel, op, dict(self.lookup, modelo=model, **self.period))
                self.assertEqual(result["resultado"]["registros"], [cells])
                scope = {**self.period, "nucleo_id":"7", "equipamento_id":"15"} if route == "/weights" else None
                self.assertEqual(result["resultado"].get("filtros_peso_aves"), scope)
                self.assertEqual(urlsplit(panel.request.call_args.args[0]).path, route)
                for call in panel.request.call_args_list:
                    self.assertEqual(len(call.args), 1)
                    self.assertFalse(call.args[0].startswith("/api/"))

    def test_no_arbitrary_report_or_equipment(self):
        for href in ("https://other.example/weights/sac?equipment_id=15", "/weights/sac?equipment_id=16",
                     "/weights/sac?equipment_id=15&switch_unit=2", "/equipment/15/edit"):
            panel = self.panel(layout(), layout(grid(card(href=href))))
            with self.assertRaises(api.AccessDenied):
                api.unit_query(panel, "consultar_historico_sac", dict(self.lookup, **self.period))
            self.assertEqual(panel.request.call_count, 2)
        form = '<form action="/weights/sac"><input name="equipment_id" value="15"></form>'
        panel = self.panel(layout(), layout(grid(card())), layout(form + table(["ontem", "outra"] + ["x"] * 6)))
        with self.assertRaises(api.AccessDenied):
            api.unit_query(panel, "consultar_historico_sac", dict(self.lookup, **self.period))

    def test_live_native_hierarchy_requires_authorized_ancestors(self):
        panel = self.panel(layout(module="/nucleis"), turbo("nuclei_id", {"7": "Núcleo A"}))
        with self.assertRaises(api.AccessDenied):
            api.unit_query(panel, "consultar_cadastros", {"nucleo_id": "9"})
        panel = self.panel(layout(module="/nucleis"), turbo("nuclei_id", {"7": "Núcleo A"}),
                           turbo("sector_id", {"22": "Galpão A"}), turbo("side_id", {"2": "A"}),
                           turbo("side_box_id", {"88": "Box B"}))
        result = api.unit_query(panel, "consultar_cadastros", {"nucleo_id": "7", "galpao_id": "22", "lado_id": "2"})
        self.assertEqual(result["resultado"], {"boxes": {"88": "Box B"}})
        call = panel.request.call_args
        self.assertEqual(call.kwargs["accept"], "text/vnd.turbo-stream.html")
        self.assertEqual(parse_qs(urlsplit(call.args[0]).query)["units"], ["1"])

    def test_period_reports_require_allowed_nucleus(self):
        for op, module in [("resumir_pesagens", "/reports"), ("consultar_alteracoes_pesagens", "/weight_maintenance")]:
            options = turbo("nuclei_id", {"7": "A", "8": "B"})
            panel = self.panel(layout(module=module), options)
            self.assertEqual(api.unit_query(panel, op, self.period)["resultado"]["estado"], "escolher_nucleo")
            with self.assertRaises(api.AccessDenied):
                api.unit_query(self.panel(layout(module=module), options), op, dict(self.period, nucleo_id="9"))

    def test_frontend_summary_and_change_rows(self):
        choice = '<select id="nuclei_id" name="nuclei_id"><option value="7" selected>A</option></select>'
        summary = '<form action="/reports"><input name="tab" value="basic"></form><p class="text-lg font-bold text-secondary-900">126 aves</p>'
        panel = self.panel(layout(module="/reports"), turbo("nuclei_id", {"7":"A"}), layout(choice + summary, module="/reports"))
        result = api.unit_query(panel, "resumir_pesagens", self.period)["resultado"]
        self.assertEqual(result["total_exibido"], "126 aves")
        self.assertEqual(result["filtros_peso_aves"], {**self.period, "nucleo_id":"7"})
        cells = ['<input value="31" data-bulk-actions-target="checkbox">ontem',
                 '<div class="grid"><div><span>SAC</span></div><div><span>A</span></div></div>']
        changes = table(cells).replace('class="list-component-row-link"', 'class="list-component-row-link line-through"')
        form = '<form><input name="unit_id" value="1"></form>'
        panel = self.panel(layout(module="/weight_maintenance"), turbo("nuclei_id", {"7":"A"}),
                           layout(form + choice + changes, module="/weight_maintenance"))
        result = api.unit_query(panel, "consultar_alteracoes_pesagens", self.period)
        self.assertEqual(result["resultado"]["alteracoes"][0]["peso_id"], "31")
        self.assertEqual(parse_qs(urlsplit(panel.request.call_args.args[0]).query)["nuclei_id[]"], ["7"])

        action = '<button onclick="window.openEditHistoryModal(31)">Abrir</button>'
        edited = changes.replace("ontem", "ontem" + action)
        page = layout(form + choice + edited, module="/weight_maintenance")
        panel = self.panel(layout(module="/weight_maintenance"), turbo("nuclei_id", {"7":"A"}), page,
                           '{"history":[{"user_name":"A","changes":[{"field_label":"peso","from_value":"2","to_value":"4"}]}]}',
                           layout(module="/weight_maintenance"))
        details = api.unit_query(panel, "consultar_alteracoes_pesagens", dict(self.period, peso_id="31"))
        self.assertEqual(details["resultado"]["alteracoes"][0]["historico"][0]["campos"][0]["to_value"], "4")
        for invalid_page, weight in ((page, "99"), (page.replace("<span>A</span>", "<span>B</span>"), "31")):
            panel = self.panel(layout(module="/weight_maintenance"), turbo("nuclei_id", {"7":"A"}), invalid_page)
            with self.assertRaises(api.AccessDenied):
                api.unit_query(panel, "consultar_alteracoes_pesagens", dict(self.period, peso_id=weight))
            self.assertEqual(panel.request.call_count, 3)

    def test_connection_status_and_invalid_dates(self):
        for hours, size in [("24", 1), ("168", 0)]:
            panel = self.panel(layout(), layout(grid(card(), True)))
            result = api.unit_query(panel, "equipamentos_sem_comunicacao", {"horas": hours})["resultado"]
            self.assertEqual(len(result["equipamentos"]), size)
            self.assertEqual(result["proxima_pagina"], 2)
        panel = self.panel(layout())
        with self.assertRaises(ValueError):
            api.unit_query(panel, "consultar_historico_sac", dict(self.lookup, inicio="2026-11-01", fim="2026-12-31"))
        self.assertEqual(panel.request.call_count, 1)
