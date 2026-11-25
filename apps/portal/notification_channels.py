"""Adaptadores de canais de notificação. Cada envio faz uma única tentativa."""
import json
import re
from http.client import HTTPException
from urllib.error import URLError, HTTPError
from urllib.request import Request, build_opener

from agent_runtime import NoRedirect


def whatsapp_recipient(value):
    """Preserva o identificador usado no envio e reconhece aliases de grupo nas permissões."""
    if isinstance(value, str):
        address = value.strip()
        group = re.fullmatch(r"([0-9]{1,100})(?:@g\.us|-group)", address)
        if group:
            group_id = group[1]
            return address, [group_id + "@g.us", group_id + "-group"]
        if re.fullmatch(r"\+?[0-9]{6,15}", address):
            digits = address.lstrip("+")
            return digits, [digits]
    raise ValueError("Informe um telefone com código do país ou o ID exato do grupo.")


RECIPIENT_VALIDATORS = {"whatsapp": whatsapp_recipient}


def send_whatsapp(config, address, message):
    integration = config["integration"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", integration):
        raise ValueError("Identificador Leadfinder inválido.")
    endpoint = f"https://api2.leadfinder.com.br/forti/api/integracao/enviarMensagem/{integration}/TEXTO"
    body = json.dumps({"mensagem": message, "destinatarios": [address]}).encode()
    request = Request(endpoint, data=body, headers={
        "Authorization": config["key"], "Content-Type": "application/json",
    }, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            status = response.status
    except HTTPError as exc:
        exc.close()
        return "failed", f"Leadfinder retornou HTTP {exc.code}. Confira antes de reenviar."
    except (OSError, HTTPException, URLError):
        return "failed", "Resultado incerto. Verifique o destino antes de reenviar."
    return "done", f"Leadfinder retornou HTTP {status}. Entrega ao destinatário ainda sem confirmação."


CHANNELS = {"whatsapp": ("WhatsApp", send_whatsapp)}
