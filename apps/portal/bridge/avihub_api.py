"""Read the native AviHUB frontend with the linked user's Rails session."""
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

EXPIRED = "Sua sessão no AviHUB expirou. Vincule sua conta novamente no agente."
RESET_PASSWORD = "solicitar_redefinicao_senha_avihub"
ACCOUNTS = "listar_contas_avihub"
UNITS = "listar_unidades_avihub"
SELECT_UNIT = "selecionar_unidade_avihub"


class SessionExpired(ValueError):
    pass


class AccessDenied(ValueError):
    pass


# Module links are emitted by the native navbar after checking the current user's permissions.
REPORTS = {
    "consultar_equipamento": ("/equipment", "Busca por série e modelo completos.", ("serie", "modelo"), ("nucleo_id",)),
    "listar_equipamentos": ("/equipment", "Inventário visível no painel.", (), ("pagina", "nucleo_id")),
    "equipamentos_sem_comunicacao": ("/equipment", "Cartões sem conexão há 24 ou 168 horas, conforme o indicador nativo. Consulte as próximas páginas.", ("horas",), ("pagina", "nucleo_id")),
    "consultar_pesagens": ("/equipment", "Pesagens exibidas no relatório do equipamento.", ("serie", "modelo", "inicio", "fim"), ("pagina", "nucleo_id")),
    "consultar_sensores": ("/equipment", "Histórico exibido pelo sensor PE40.", ("serie", "inicio", "fim"), ("pagina",)),
    "consultar_racao": ("/equipment", "Dosagem e programação de SEQ ou SP501S.", ("serie", "modelo", "inicio", "fim"), ("pagina",)),
    "consultar_silos": ("/equipment", "Leituras de SILO ou SPSILO.", ("serie", "modelo", "inicio", "fim"), ("pagina",)),
    "consultar_historico_sac": ("/equipment", "Histórico de calhas pelo relatório da SAC.", ("serie", "inicio", "fim"), ("pagina",)),
    "resumir_pesagens": ("/reports", "Resumo e eventos da consulta básica de pesagens de aves por núcleo.", ("inicio", "fim"), ("nucleo_id", "pagina")),
    "consultar_alteracoes_pesagens": ("/weight_maintenance", "Edições e descartes exibidos na manutenção, por data da pesagem e núcleo. Use peso_id e a mesma página para abrir o detalhe exibido.", ("inicio", "fim"), ("nucleo_id", "pagina", "peso_id")),
    "consultar_cadastros": ("/nucleis", "Opções dos seletores nativos de núcleo, lote, galpão, lado e box.", (), ("nucleo_id", "galpao_id", "lado_id")),
}

HISTORY = {
    "consultar_pesagens": ("/weights", "/weights/incubation"),
    "consultar_historico_sac": ("/weights/sac",),
    "consultar_sensores": ("/weights/pe40",),
    "consultar_racao": ("/weights/ration", "/weights/sp501s"),
    "consultar_silos": ("/weights/silo", "/weights/spsilo"),
}
FIXED_MODELS = {"consultar_historico_sac": "SAC", "consultar_sensores": "PE40"}
TIMELINE_TYPES = {
    "border-l-emerald-500": "selecao",
    "border-l-indigo-500": "coleta_automatica",
    "border-l-blue-500": "amostragem",
}
TIMELINE_IDS = {
    "lote_id": "batch_id", "galpao_id": "sector_id", "lado_id": "side_id",
    "box_id": "side_box_id", "equipamento_id": "equipment_id",
}


def select_account(accounts, selection):
    if not accounts:
        raise AccessDenied("Vincule sua conta no agente AviHUB do portal.")
    if selection:
        key = selection.strip().casefold()
        if re.fullmatch(r"conta-[0-9]+", key):
            matches = [a for a in accounts if a["id"] == key]
        else:
            matches = [a for a in accounts if key and key in (a["nome"].casefold(), a["email"].casefold())]
        if not matches:
            raise AccessDenied("Conta indisponível. Consulte listar_contas_avihub para identificar a sessão.")
    else:
        matches = accounts
    if len(matches) == 1:
        return matches[0]
    return {"estado": "escolher_conta", "contas": matches,
            "mensagem": "Use a conta identificada pelo usuário no contexto desta conversa. "
                        "Se houver dúvida, apresente as contas e pergunte qual usar antes de consultar. "
                        "Passe o ID da conta nas próximas consultas desse contexto."}


def select_unit(units, selection):
    if not units:
        raise AccessDenied("Nenhuma unidade autorizada.")
    if not selection:
        return units[0] if len(units) == 1 else None
    if not isinstance(selection, str):
        raise ValueError("Informe o ID ou o nome da unidade.")
    matches = [u for u in units if u["id"] == selection] or [
        u for u in units if u["nome"].casefold() == selection.strip().casefold()]
    if len(matches) != 1:
        raise AccessDenied("Unidade indisponível ou nome ambíguo. Use o ID de listar_unidades_avihub.")
    return matches[0]


def unit_query(panel, operation, args):
    doc = panel.home()
    units = panel.units(doc)
    if operation == UNITS:
        return {"unidades": units}
    if operation == SELECT_UNIT and not args.get("unidade"):
        raise ValueError("Informe a unidade escolhida pelo usuário.")
    unit = select_unit(units, args.get("unidade"))
    if unit is None:
        return {"estado": "escolher_unidade", "unidades": units,
                "mensagem": "Pergunte qual unidade deseja usar. Informe que, ao ativá-la, ela também ficará selecionada no painel AviHUB. Aguarde a escolha."}
    if operation == SELECT_UNIT:
        return panel.activate(unit)
    module, _, required, optional = REPORTS[operation]
    panel.require_context(doc, unit["id"], module)
    params = {}
    for key in (*required, *optional):
        value = args.get(key)
        if value is None or value == "":
            if key in required:
                raise ValueError("Informe " + key + ".")
            continue
        if key.endswith("_id") or key in ("horas", "pagina"):
            if not re.fullmatch(r"[1-9][0-9]{0,17}", str(value)):
                raise ValueError("Informe um número positivo para " + key + ".")
            value = str(value)
        elif not isinstance(value, str) or not value.strip() or len(value) > 80 or not value.isprintable():
            raise ValueError("Valor inválido para " + key + ".")
        params[key] = value.strip()
    if "inicio" in params:
        try:
            start, end = (date.fromisoformat(params[k]) for k in ("inicio", "fim"))
        except ValueError:
            raise ValueError("Informe datas no formato AAAA-MM-DD.") from None
        if not 0 <= (end - start).days <= 30:
            raise ValueError("Informe um período de até 31 dias.")
        params.update(inicio=start.isoformat(), fim=end.isoformat())
    page = int(params.pop("pagina", 1))
    if module == "/equipment":
        result = equipment_query(panel, unit, operation, params, page)
    elif module == "/nucleis":
        result = catalog(panel, unit, params)
    else:
        result = period_report(panel, unit, module, params, page)
    if operation == "consultar_equipamento":
        return {**result, "unidade": unit["nome"], "unidade_id": unit["id"]}
    return {"unidade": unit, "resultado": result}


def next_page(doc, page):
    more = doc.select_one('.list-pagination a[rel~="next"], .list-pagination .next a')
    return page + 1 if more else None


def card_data(card):
    equipment_id = card.get("data-equipment-id", "")
    serial = card.get("data-equipment-serie")
    model = card.get("data-equipment-model")
    if not re.fullmatch(r"[0-9]+", equipment_id) or not serial or not model:
        raise ValueError("O painel retornou um cartão de equipamento incompleto.")
    location = card.select_one(".equipment-location")
    connection = card.find(id=f"equipment_{equipment_id}_last_connection_frame")
    return {"id": equipment_id, "serie": serial, "modelo": model, "versao": card.get("data-equipment-version"),
            "localizacao": location.get_text(" ", strip=True) if location else None,
            "ultima_conexao": connection.get_text(" ", strip=True) if connection else None}


def cards(panel, unit, params, page):
    query = {"page": page}
    if "serie" in params:
        query["search"] = params["serie"]
    if "nucleo_id" in params:
        query["nuclei_id"] = params["nucleo_id"]
    doc = panel.page("/equipment", query, unit["id"], "/equipment")
    grid = doc.find(attrs={"data-equipment-realtime-unit-id-value": unit["id"]})
    if grid is None:
        raise AccessDenied("O painel não confirmou o inventário da unidade.")
    return grid.select(".equipment-card"), next_page(doc, page)


def exact_card(panel, unit, params, model):
    page, matches = 1, []
    while page:
        batch, page = cards(panel, unit, params, page)
        for card in batch:
            eq = card_data(card)
            if eq["serie"].casefold() == params["serie"].casefold() and eq["modelo"].casefold() == model.casefold():
                matches.append((eq, card))
                if len(matches) > 1:
                    raise ValueError("Mais de um equipamento com essa série e modelo. Informe o núcleo.")
    if not matches:
        raise ValueError("Equipamento não encontrado no inventário autorizado desta unidade.")
    return matches[0]


def history_path(card, equipment_id, paths):
    for link in card.select("a[href]"):
        url = urlsplit(link["href"])
        if not url.scheme and not url.netloc and url.path in paths and parse_qs(url.query) == {"equipment_id": [equipment_id]}:
            return url.path
    raise AccessDenied("O painel não oferece esse relatório para o equipamento.")


def table(doc):
    header = doc.select_one(".list-component .list-component-header")
    if header is None:
        raise ValueError("O painel não apresentou a tabela esperada.")
    body = header.find_parent(class_="list-component")
    cols = [c.get_text(" ", strip=True) for c in header.find_all(recursive=False)]
    rows = body.select(".list-component-row .list-component-row-link")
    if not cols or (not rows and not body.select_one(".list-component-empty")):
        raise ValueError("O painel retornou uma tabela em formato inesperado.")
    return cols, rows


def equipment_query(panel, unit, op, params, page):
    if op in ("listar_equipamentos", "equipamentos_sem_comunicacao"):
        batch, following = cards(panel, unit, params, page)
        if op == "equipamentos_sem_comunicacao":
            hours = params["horas"]
            if hours not in ("24", "168"):
                raise ValueError("O indicador do painel oferece os limites de 24 e 168 horas.")
            selector = ".text-danger-500, .text-yellow-500" if hours == "24" else ".text-danger-500"
            stale = []
            for card in batch:
                frame = card.select_one('turbo-frame[id$="_last_connection_frame"]')
                if frame and frame.select_one(selector):
                    stale.append(card)
            batch = stale
        return {"equipamentos": [card_data(c) for c in batch], "proxima_pagina": following}
    eq, card = exact_card(panel, unit, params, FIXED_MODELS.get(op, params.get("modelo")))
    if op == "consultar_equipamento":
        return eq
    path = history_path(card, eq["id"], HISTORY[op])
    filters = {"equipment_id": eq["id"], "start_date": params["inicio"], "end_date": params["fim"], "page": page}
    if path == "/weights":
        nucleus = select_nucleus(panel, unit, params)
        if isinstance(nucleus, dict):
            return nucleus
        filters["nuclei_id"] = nucleus
    doc = panel.page(path, filters, unit["id"], "/equipment")
    form = doc.find("form", action=path)
    field = form and form.find("input", attrs={"name": "equipment_id"})
    if not field or field.get("value") != eq["id"]:
        raise AccessDenied("O relatório não confirmou o filtro de equipamento.")
    labels, rows = table(doc)
    readings = []
    for row in rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(recursive=False)]
        if len(cells) != len(labels):
            raise ValueError("As colunas do relatório mudaram.")
        if path == "/weights/sac":
            same_device = len(cells) == 8 and cells[1] == eq["serie"]
        else:
            same_device = cells[0] in (eq["modelo"] + "/" + eq["serie"], eq["modelo"] + " / " + eq["serie"])
        if not same_device:
            raise AccessDenied("O relatório retornou dados de outro equipamento.")
        readings.append(cells)
    result = {"equipamento": eq, "colunas": labels, "registros": readings, "proxima_pagina": next_page(doc, page)}
    if path == "/weights":
        result["filtros_peso_aves"] = {"inicio": params["inicio"], "fim": params["fim"],
                                       "nucleo_id": nucleus, "equipamento_id": eq["id"]}
    return result


def select_nucleus(panel, unit, params):
    nuclei = panel.options("nuclei_id", {"units": unit["id"]})
    selected = params.get("nucleo_id")
    if selected and selected not in nuclei:
        raise AccessDenied("Núcleo indisponível nesta unidade.")
    if selected or len(nuclei) == 1:
        return selected or next(iter(nuclei))
    if not nuclei:
        raise AccessDenied("Nenhum núcleo disponível para esta consulta.")
    return {"estado": "escolher_nucleo", "nucleos": nuclei}


def catalog(panel, unit, params):
    scope = {"units": unit["id"]}
    nuclei = panel.options("nuclei_id", scope)
    if not params:
        return {"nucleos": nuclei}
    if params.get("nucleo_id") not in nuclei:
        raise AccessDenied("Escolha um núcleo listado nesta unidade.")
    scope["nucleis"] = params["nucleo_id"]
    sectors = panel.options("sector_id", scope)
    if not params.get("galpao_id") and not params.get("lado_id"):
        return {"lotes": panel.options("batch_id", scope), "galpoes": sectors}
    if params.get("galpao_id") not in sectors:
        raise AccessDenied("Escolha um galpão listado neste núcleo.")
    scope["sectors"] = params["galpao_id"]
    sides = panel.options("side_id", scope)
    if not params.get("lado_id"):
        return {"lados": sides}
    if params["lado_id"] not in sides:
        raise AccessDenied("Escolha um lado listado neste galpão.")
    return {"boxes": panel.options("side_box_id", {**scope, "sides": params["lado_id"]})}


def timeline_values(args, key):
    return args.get(key, []) + args.get(key + "[]", [])


def timeline_report_event(event, unit_id, nucleus, period):
    # The native card links to a report preview. Read evidenced filters without following that link.
    try:
        target = urlsplit(event["href"])
        args = parse_qs(target.query, keep_blank_values=True, max_num_fields=200)
        start, end = (datetime.strptime(args[key][0], "%d/%m/%Y").date().isoformat() for key in ("start_date", "end_date"))
    except (KeyError, ValueError):
        return None
    if (target.scheme or target.netloc or target.fragment or target.path != "/reports"
            or args.get("tab") != ["advanced"] or args.get("birds") != ["true"] or args.get("report_type") != ["pdf"]
            or timeline_values(args, "unit_id") != [unit_id] or timeline_values(args, "nuclei_id") != [nucleus]
            or len(args["start_date"]) != 1 or len(args["end_date"]) != 1
            or not period["inicio"] <= start == end <= period["fim"]):
        return None
    event_type = next((TIMELINE_TYPES[cls] for cls in event.get("class", ()) if cls in TIMELINE_TYPES), None)
    if not event_type:
        return None
    filters = {"inicio":start, "fim":end, "nucleo_id":nucleus}
    multi = {}
    for name, native in TIMELINE_IDS.items():
        values = timeline_values(args, native)
        if any(not re.fullmatch(r"[1-9][0-9]{0,17}", value) for value in values):
            return None
        if len(values) == 1:
            filters[name] = values[0]
        elif values:
            multi[name] = values
    balance_types = args.get("balance_type", [])
    if len(balance_types) == 1 and re.fullmatch(r"[1-9][0-9]{0,17}|CS_RECRIA|imported_file", balance_types[0]):
        filters["tipo_balanca"] = balance_types[0]
    genders = args.get("gender_cd", [])
    if len(genders) == 1 and genders[0] in ("F", "M", "Z"):
        filters["sexo"] = genders[0]
    if args.get("visualization_type") == ["daily"]:
        filters["visualizacao"] = "daily"
    result = {"tipo":event_type, "descricao":event.get_text(" ", strip=True), "filtros":filters}
    if multi:
        result["filtros_multiplos"] = multi
    return result


def period_report(panel, unit, module, params, page):
    nucleus = select_nucleus(panel, unit, params)
    if isinstance(nucleus, dict):
        return nucleus
    filters = {"nuclei_id": nucleus, "start_date": params["inicio"], "end_date": params["fim"]}
    if module == "/reports":
        doc = panel.page(module, {**filters, "tab": "basic"}, unit["id"], module)
        form = doc.select_one('form[action="/reports"] input[name="tab"][value="basic"]')
        if not form or not doc.select_one('select[name="nuclei_id"] option[selected][value="' + nucleus + '"]'):
            raise AccessDenied("O relatório não confirmou o núcleo selecionado.")
        summary = doc.select_one(".text-lg.font-bold.text-secondary-900")
        if not summary:
            raise ValueError("O painel não apresentou o resumo do período.")
        events = doc.select('a.group.border-l-4[href^="/reports?"]')
        start = (page - 1) * 25
        selected = events[start:start + 25]
        return {"total_exibido": summary.get_text(" ", strip=True),
                "eventos": [event.get_text(" ", strip=True) for event in selected],
                "eventos_peso_aves": [parsed for event in selected
                                     if (parsed := timeline_report_event(event, unit["id"], nucleus, params))],
                "proxima_pagina": page + 1 if start + 25 < len(events) else None,
                "filtros_peso_aves": {"inicio": params["inicio"], "fim": params["fim"], "nucleo_id": nucleus}}
    filters["nuclei_id[]"] = filters.pop("nuclei_id")
    doc = panel.page(module, {**filters, "page": page, "status": "all"}, unit["id"], module)
    field = doc.select_one('form input[name="unit_id"]')
    if not field or field.get("value") != unit["id"]:
        raise AccessDenied("A tela de manutenção não confirmou a unidade.")
    selected = doc.select_one('select[id="nuclei_id"] option[selected][value="' + nucleus + '"]')
    if not selected:
        raise AccessDenied("A manutenção não confirmou o núcleo selecionado.")
    labels, rows = table(doc)
    changed = []
    for row in rows:
        checkbox = row.select_one('input[data-bulk-actions-target="checkbox"]')
        row_id = checkbox.get("value", "") if checkbox else ""
        if not re.fullmatch(r"[0-9]+", row_id):
            raise ValueError("A tela não confirmou o identificador da pesagem.")
        button = row.find("button", onclick=f"window.openEditHistoryModal({row_id})")
        if button is None and "line-through" not in row.get("class", []):
            continue
        columns = row.find_all(recursive=False)
        cells = [c.get_text(" ", strip=True) for c in columns]
        if len(cells) != len(labels) or len(columns) < 2:
            raise ValueError("As colunas do histórico de alterações mudaram.")
        locations = columns[1].select(".grid > div > span")
        if len(locations) < 2 or locations[1].get_text(strip=True) != selected.get_text(strip=True):
            raise AccessDenied("A manutenção não confirmou o núcleo da pesagem.")
        record = {"peso_id": row_id, "valores": cells, "tem_detalhes": button is not None}
        if params.get("peso_id") == row_id and button is not None:
            payload = panel.object(panel.content(f"/weight_maintenance/{row_id}/edit_history", "application/json"))
            panel.require_context(panel.home(), unit["id"], module)
            history = payload.get("history")
            if not isinstance(history, list):
                raise ValueError("O painel não retornou os detalhes da alteração.")
            entries = []
            for entry in history:
                if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
                    raise ValueError("O painel retornou uma alteração em formato inesperado.")
                entries.append({"usuario": entry.get("user_name"), "data": entry.get("timestamp"),
                                "campos": [{key: change.get(key) for key in ("field_label", "from_value", "to_value")}
                                           for change in entry["changes"] if isinstance(change, dict)]})
            record["historico"] = entries
        if not params.get("peso_id") or params["peso_id"] == row_id:
            changed.append(record)
    if params.get("peso_id") and not changed:
        raise AccessDenied("A pesagem não consta entre as alterações desta página autorizada.")
    return {"colunas": labels, "alteracoes": changed, "proxima_pagina": next_page(doc, page)}
