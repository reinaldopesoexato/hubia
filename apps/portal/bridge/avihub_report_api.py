"""Native Peso Aves report generation and download with a linked AviHUB session."""

import json
import re
from base64 import b64encode
from datetime import date
from email.message import Message
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .avihub_api import AccessDenied, EXPIRED, SessionExpired, select_unit
from .avihub_client import LOGIN, MFA
from .avihub_pdf import render_pdf

REPORTS = "/reports"
ADVANCED = REPORTS + "?tab=advanced"
NOTIFICATIONS = "/notifications.json"
PDF_FORMATS = frozenset({"pdf", "pdf_summary"})
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORMATS = {
    "excel_aves": ("xlsx", XLSX),
    "excel_amostra_completa": ("xlsx", XLSX),
    "excel_resumo_aves": ("xlsx", XLSX),
    "excel_box_aves": ("xlsx", XLSX),
    "csv_aves": ("csv", "text/csv"),
    "csv_aves_v2": ("csv", "text/csv"),
    "csv_aves_multiblocos": ("csv", "text/csv"),
    "pdf": ("pdf", "application/pdf"),
    "pdf_summary": ("pdf", "application/pdf"),
    "txt_aves": ("txt", "text/plain"),
    "agrosys_csv_aves": ("csv", "text/csv"),
    "avecom_aves": ("txt", "text/plain"),
    "avecom_2_aves": ("csv", "text/csv"),
    "avecom_3_aves": ("txt", "text/plain"),
    "pesagens_periodo_csv": ("csv", "text/csv"),
    "agromanager_txt_aves": ("txt", "text/plain"),
}
FORMAT_ALIASES = {"excel": "excel_aves", "xlsx": "excel_aves", "csv": "csv_aves", "txt": "txt_aves"}
NATIVE_ID = re.compile(r"[1-9][0-9]{0,18}")
NATIVE_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
MAX_OPTION_ITEMS = 100
FILE_LIMIT = 24 * 1024 * 1024
PREVIEW_IMAGE = re.compile(r"/assets/(?:logo|qr_code)(?:-[a-f0-9]+)?\.png")

# Field names in the advanced Peso Aves form and /load_children query scopes.
SCOPED_FILTERS = {
    "nucleo_id": ("nuclei_id", ()),
    "lote_id": ("batch_id", (("nucleis", "nucleo_id"),)),
    "galpao_id": ("sector_id", (("nucleis", "nucleo_id"),)),
    "lado_id": ("side_id", (("nucleis", "nucleo_id"), ("sectors", "galpao_id"))),
    "box_id": ("side_box_id", (("nucleis", "nucleo_id"), ("sectors", "galpao_id"), ("sides", "lado_id"))),
    "equipamento_id": ("equipment_id", ()),
}
SELECT_FILTERS = {
    "sexo": "gender_cd",
    "tipo_balanca": "balance_type",
    "periodo_pesagem": "weighing_schedule",
    "visualizacao": "visualization_type",
}
FILTER_FIELDS = ("inicio", "fim", "formato", *SCOPED_FILTERS, *SELECT_FILTERS, "hora_inicio", "hora_fim")


class SubmissionUncertain(ValueError):
    """The submit may have succeeded. Callers must not repeat it automatically."""

    def __init__(self):
        super().__init__("O AviHUB não confirmou a geração. Verifique a Central de Notificações antes de tentar novamente.")


class NativePdfUnavailable(ValueError):
    """The AviHUB installation returned its legacy print preview for a PDF request."""


def form_select(form, name):
    return form.find("select", attrs={"name": [name, name + "[]"]})


def native_options(select):
    if select is None or select.has_attr("disabled"):
        return {}
    return {opt["value"]: opt.get_text(" ", strip=True) for opt in select.select("option[value]")
            if opt["value"] and not opt.has_attr("disabled") and not opt.find_parent("optgroup", disabled=True)}


def listed_options(choices, selected=None):
    keys = list(choices)[:MAX_OPTION_ITEMS]
    if selected in choices and selected not in keys:
        keys[-1] = selected
    return [{"id": key, "nome": choices[key]} for key in keys]


def report_form(panel, unit_id):
    doc = panel.page(REPORTS, {"tab": "advanced"}, unit_id, REPORTS)
    form = doc.select_one('form[action="/reports"][method="post"]:has(input[name="birds"][value="true"])')
    if not form or unit_id not in native_options(form_select(form, "unit_id")):
        raise AccessDenied("O AviHUB não confirmou o formulário Peso Aves para a unidade ativa.")
    return doc, form


def authenticity_token(doc, form):
    field = form.select_one('input[name="authenticity_token"][value]')
    if field and field["value"]:
        return field["value"]
    # Rails accepts the masked global token in the same form parameter.
    if doc.select_one('head meta[name="csrf-param"][content="authenticity_token"]'):
        meta = doc.select_one('head meta[name="csrf-token"][content]')
        if meta and meta["content"]:
            return meta["content"]
    raise AccessDenied("O AviHUB não confirmou o token de geração do relatório Peso Aves.")


def requested_filters(args):
    params = {}
    for key in FILTER_FIELDS:
        value = args.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.isprintable() or not value.strip() or len(value) > 120:
            raise ValueError(f"Informe um valor válido para {key}.")
        value = value.strip()
        if key in SCOPED_FILTERS:
            ids = [item.strip() for item in value.split(",")]
            if (key == "nucleo_id" and len(ids) != 1
                    or len(ids) != len(set(ids))
                    or any(not NATIVE_ID.fullmatch(item) for item in ids)):
                raise ValueError(f"Informe os IDs de {key} mostrados nas opções do AviHUB.")
            value = ",".join(sorted(ids, key=int))
        params[key] = value
    if "formato" in params:
        given = params["formato"].casefold()
        params["formato"] = FORMAT_ALIASES.get(given, given)
    return params


def check_period(params, *, required):
    if required and not {"inicio", "fim"} <= params.keys():
        raise ValueError("Informe as datas inicial e final do relatório Peso Aves.")
    parsed = {}
    for key in ("inicio", "fim"):
        raw = params.get(key)
        if raw is not None:
            try:
                day = date.fromisoformat(raw)
                if day.isoformat() != raw:
                    raise ValueError
            except ValueError:
                raise ValueError("Informe datas no formato AAAA-MM-DD.") from None
            parsed[key] = day
    if len(parsed) == 2 and parsed["inicio"] > parsed["fim"]:
        raise ValueError("A data final precisa ser igual ou posterior à data inicial.")

    interval = params.get("periodo_pesagem") == "interval"
    hours = (params.get("hora_inicio"), params.get("hora_fim"))
    if required and interval and not all(hours):
        raise ValueError("Informe a hora inicial e a final do intervalo de pesagem.")
    if any(hours) and not interval:
        raise ValueError("Use o período de pesagem 'interval' para informar horas.")
    if any(value and not NATIVE_TIME.fullmatch(value) for value in hours):
        raise ValueError("Informe as horas no formato HH:MM.")
    if all(hours) and (hours[0] == hours[1] or (hours[0] > hours[1] and len(parsed) == 2
                                               and parsed["inicio"] == parsed["fim"])):
        raise ValueError("Escolha horas diferentes e informe a data seguinte se o intervalo virar a meia-noite.")


def scoped_filter(panel, form, field, unit_id, params):
    name, ancestors = SCOPED_FILTERS[field]
    if any(not params.get(key) for _, key in ancestors):
        if field in params:
            missing = ", ".join(key for _, key in ancestors if not params.get(key))
            raise ValueError(f"Informe {missing} antes de escolher {field}.")
        return {}
    selector = form_select(form, name)
    if selector is None or not selector.get("id") or selector.has_attr("disabled"):
        if field in params:
            raise AccessDenied(f"O AviHUB não oferece o filtro {field} para a unidade.")
        return {}
    if "," in params.get(field, "") and (selector["name"] != name + "[]" or not selector.has_attr("multiple")):
        raise AccessDenied(f"O AviHUB não oferece seleção múltipla para {field}.")
    scope = {"units": unit_id, **{key: params[value] for key, value in ancestors}}
    if field == "equipamento_id":
        if "nucleo_id" in params:
            scope["nucleis"] = params["nucleo_id"]
        if "tipo_balanca" in params:
            scope["models"] = params["tipo_balanca"]
    return panel.options(selector["id"], scope)


def _prepare(panel, args, for_generation):
    doc = panel.home()
    units = panel.units(doc)
    unit = select_unit(units, args.get("unidade"))
    if unit is None:
        return {"estado": "escolher_unidade", "unidades": units,
                "mensagem": "Pergunte qual unidade usar. Para gerar o relatório, ela precisa estar ativa no AviHUB."}, None
    panel.require_context(doc, unit["id"], REPORTS)
    doc, form = report_form(panel, unit["id"])
    formats = {key: label for key, label in native_options(form_select(form, "report_type")).items() if key in FORMATS}
    if not formats:
        raise AccessDenied("O AviHUB não retornou formatos disponíveis para Peso Aves.")
    params = requested_filters(args)
    check_period(params, required=for_generation)
    output_format = params.get("formato")
    if output_format is not None and output_format not in formats:
        raise ValueError("Escolha um dos formatos de relatório disponíveis no AviHUB.")
    if for_generation and not output_format:
        raise ValueError("Informe o formato solicitado para o relatório Peso Aves.")

    filters = {}
    for field, native in SELECT_FILTERS.items():
        choices = native_options(form_select(form, native))
        if field in params and params[field] not in choices:
            raise ValueError(f"O valor de {field} não aparece entre as opções do AviHUB.")
        filters[field] = listed_options(choices, params.get(field))
    scoped_fields = {}
    for field, (native, _) in SCOPED_FILTERS.items():
        value = params.get(field)
        if for_generation and value is None:
            continue
        choices = scoped_filter(panel, form, field, unit["id"], params)
        if value is not None:
            selected_ids = value.split(",")
            if any(item not in choices for item in selected_ids):
                raise AccessDenied(f"O filtro {field} não está disponível nesta unidade ou seleção.")
            if len(selected_ids) > 1:
                choices = {**{item: choices[item] for item in selected_ids}, **choices}
                scoped_fields[native + "[]"] = selected_ids
            else:
                scoped_fields[native + "[]"] = value
        filters[field] = listed_options(choices, value)

    result = {"estado": "opcoes", "unidade": unit, "filtros": filters,
              "formatos": [{"id": key, "nome": label, "extensao": FORMATS[key][0], "modo": "arquivo"}
                           for key, label in formats.items()]}
    if not for_generation:
        return result, None
    result["parametros"] = {"unidade": unit["id"], **params}
    fields = {"authenticity_token": authenticity_token(doc, form), "birds": "true", "unit_id[]": unit["id"],
              "report_type": output_format, "start_date": params["inicio"], "end_date": params["fim"]}
    fields.update(scoped_fields)
    fields.update({native: params[field] for field, native in SELECT_FILTERS.items() if field in params})
    fields.update({native: params[field] for field, native in (("hora_inicio", "start_time"), ("hora_fim", "end_time"))
                   if field in params})
    return result, fields


def prepare(panel, args, *, for_generation=False):
    result, _ = _prepare(panel, args, for_generation)
    return result


def require_download_access(panel, unit_id):
    panel.require_context(panel.home(), unit_id, REPORTS)


def notification_matches(panel, item, report_id):
    if not isinstance(item, dict) or not NATIVE_ID.fullmatch(str(item.get("id", ""))):
        return False
    params = item.get("params")
    if isinstance(params, str):
        try:
            params = panel.object(params)
        except ValueError:
            return False
    if not isinstance(params, dict) or params.get("notification_context") != "reports_completed":
        return False
    href = params.get("url")
    if not isinstance(href, str):
        return False
    try:
        url = urlsplit(urljoin(panel.origin + "/", href))
    except ValueError:
        return False
    return (f"{url.scheme}://{url.netloc}" == panel.origin and url.path == REPORTS
            and parse_qs(url.query, keep_blank_values=True).get("report_id") == [str(report_id)])


def find_notification(panel, unit_id, report_id):
    """Find the notification for the confirmed POST, scoped to the same AviHUB session."""
    if not NATIVE_ID.fullmatch(str(report_id)):
        raise ValueError("O identificador do relatório não foi confirmado pelo AviHUB.")
    require_download_access(panel, unit_id)
    for page in range(1, 4):
        query = urlencode({"search":f"report_id={report_id}", "page":page})
        body = panel.content(f"{NOTIFICATIONS}?{query}", "application/json")
        try:
            items = json.loads(body)
        except (UnicodeError, ValueError):
            raise ValueError("O AviHUB não apresentou as notificações do usuário.") from None
        if not isinstance(items, list):
            raise ValueError("O AviHUB não apresentou as notificações do usuário.")
        for item in items:
            if notification_matches(panel, item, report_id):
                return str(item["id"])
        if len(items) < 20:
            break
    return None


def generate(panel, args):
    _, fields = _prepare(panel, args, True)
    if fields is None:
        raise AccessDenied("Escolha a unidade ativa antes de gerar o relatório Peso Aves.")
    try:
        status, location, _, _ = panel.exchange(REPORTS, fields, referer=ADVANCED, read_timeout=90)
    except ValueError as exc:
        raise SubmissionUncertain from exc
    destination = urlsplit(location or "")
    if status == 401 or (status in (302, 303) and destination.path in (LOGIN, MFA)):
        raise SessionExpired(EXPIRED)
    if status == 403 or (status in (302, 303) and destination.path == "/403"):
        raise AccessDenied("Sua conta não pode gerar relatórios Peso Aves na unidade.")
    query = parse_qs(destination.query)
    ids = query.get("report_id", [])
    if (status not in (302, 303) or destination.scheme or destination.netloc or destination.fragment
            or destination.path != REPORTS or query.get("tab") != ["advanced"]
            or len(ids) != 1 or not NATIVE_ID.fullmatch(ids[0])):
        raise SubmissionUncertain
    return ids[0]


def preview_document(panel, unit_id, report_id):
    if not NATIVE_ID.fullmatch(str(report_id)):
        raise ValueError("O identificador do relatório não foi confirmado pelo AviHUB.")
    params = {"report_id": str(report_id), "tab": "advanced"}
    doc = panel.page(REPORTS, params, unit_id, REPORTS)
    frames = doc.select("iframe#report-iframe[srcdoc]")
    if len(frames) != 1 or not frames[0]["srcdoc"].strip():
        raise AccessDenied("O AviHUB não confirmou a pré-visualização do relatório.")
    return frames[0]["srcdoc"]


def preview_url(panel, unit_id, report_id):
    preview_document(panel, unit_id, report_id)
    params = {"report_id": str(report_id), "tab": "advanced"}
    return panel.origin + REPORTS + "?" + urlencode(params)


def preview_images(panel, html):
    doc = BeautifulSoup(html, "html.parser")
    local_images = {}
    for image in doc.select("img[src]"):
        src = image["src"].strip()
        if src.startswith("data:image/"):
            continue
        url = urlsplit(urljoin(panel.origin + REPORTS, src))
        if (f"{url.scheme}://{url.netloc}" != panel.origin or url.query or url.fragment
                or not PREVIEW_IMAGE.fullmatch(url.path)):
            raise ValueError("O AviHUB retornou uma imagem não disponível para a impressão do relatório.")
        if url.path not in local_images:
            status, _, headers, body = panel.exchange(url.path, accept="image/png", limit=2 * 1024 * 1024)
            if (status != 200 or headers.get("Content-Type", "").split(";", 1)[0] != "image/png"
                    or not body.startswith(b"\x89PNG\r\n\x1a\n")):
                raise ValueError("O AviHUB não disponibilizou uma imagem do relatório para impressão.")
            local_images[url.path] = "data:image/png;base64," + b64encode(body).decode("ascii")
        image["src"] = local_images[url.path]
    return str(doc) if local_images else html


def print_preview(panel, unit_id, report_id):
    html = preview_document(panel, unit_id, report_id)
    data = render_pdf(preview_images(panel, html))
    if not data.startswith(b"%PDF-") or len(data) > FILE_LIMIT:
        raise ValueError("A impressão da prévia do AviHUB não retornou um arquivo PDF válido.")
    return f"peso-aves-{report_id}.pdf", "application/pdf", data


def download(panel, unit_id, report_id, report_format):
    if report_format not in FORMATS or not NATIVE_ID.fullmatch(str(report_id)):
        raise ValueError("O formato não está disponível para download direto.")
    require_download_access(panel, unit_id)
    extension, mime_type = FORMATS[report_format]
    pdf = report_format in PDF_FORMATS
    path = f"{REPORTS}/{report_id}/download" + (".pdf" if pdf else "")
    status, location, headers, body = panel.exchange(path, accept=mime_type,
                                                     read_timeout=30, limit=FILE_LIMIT)
    if status == 401 or (status in (302, 303) and urlsplit(location or "").path in (LOGIN, MFA)):
        raise SessionExpired(EXPIRED)
    if pdf and status in (302, 303):
        preview = urlsplit(location or "")
        if preview.path == REPORTS and parse_qs(preview.query).get("report_id") == [str(report_id)]:
            raise NativePdfUnavailable
    content_type = headers.get("Content-Type", "")
    disposition = Message()
    disposition["Content-Disposition"] = headers.get("Content-Disposition", "")
    if (status != 200 or not body or not content_type.isascii() or not content_type.isprintable()
            or len(content_type) > 150 or content_type.split(";", 1)[0].strip().lower() != mime_type
            or (pdf and not body.startswith(b"%PDF-"))
            or disposition.get_content_disposition() != "attachment"):
        raise AccessDenied("O AviHUB não confirmou um arquivo no formato solicitado para este relatório.")
    filename = (disposition.get_filename() or "").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char.isprintable()).strip()
    if len(filename) > 200 or not filename.casefold().endswith("." + extension):
        filename = f"peso-aves-{report_id}.{extension}"
    return filename, content_type, body
