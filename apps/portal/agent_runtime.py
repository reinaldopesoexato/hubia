"""Executores disponíveis para agentes do portal."""
from http.client import HTTPException
import json
import re
import unicodedata
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit



class ConnectorError(Exception):
    pass


def connectors_for(tenant, space):
    location = Path("/run/hubia/connectors.json")
    if not location.is_file():
        return {}
    try:
        registry = json.loads(location.read_text())
        entries = registry.get(str(tenant), {}).get(str(space), {})
        if isinstance(entries, dict):
            return entries
    except (ValueError, OSError, AttributeError):
        pass
    raise ConnectorError("O cadastro de conectores no servidor precisa de revisão.")


def https_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.port == 0 or
            parts.username is not None or parts.password is not None or parts.fragment or
            "\\" in value or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("Informe um endereço HTTPS sem credenciais ou fragmento.")
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_connector(config, context, question, run_id, history=()):
    try:
        endpoint = https_url(config["url"])
        token = config.get("token")
        model_chat = config.get("type") == "chat_completions"
        if model_chat:
            messages = []
            if config.get("instructions"):
                messages.append({"role":"system","content":config["instructions"]})
            for previous_question, previous_answer in history:
                messages.extend([{"role":"user","content":previous_question},
                                 {"role":"assistant","content":previous_answer}])
            messages.append({"role":"user","content":question})
            fields = {"model":config["model"],"messages":messages,"max_completion_tokens":1200,"store":False}
        else:
            fields = {**context, "query": question, "request_id": str(run_id)}
        body = json.dumps(fields).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = request.Request(endpoint, data=body, headers=headers, method="POST")
        with request.build_opener(NoRedirect()).open(payload, timeout=8) as response:
            data = response.read(65537)
        if len(data) <= 65536:
            result = json.loads(data)
            answer = result["choices"][0]["message"]["content"] if model_chat else result["answer"]
            if isinstance(answer, str) and 0 < len(answer.strip()) <= 16000:
                return answer.strip()
    except (ValueError, TypeError, IndexError, AttributeError, KeyError, HTTPException, OSError, error.URLError):
        pass
    raise ConnectorError("O conector não retornou uma resposta válida. Verifique o serviço na administração.")


def terms(value):
    plain = unicodedata.normalize("NFKD", value.casefold()).encode("ascii","ignore").decode()
    return set(re.findall(r"[a-z0-9]+", plain)) - {"a","as","o","os","e","de","do","da","dos","das","no","na","um","uma","como","qual","que","para"}


def search_knowledge(text, question):
    words = terms(question)
    sections = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    matches = sorted(((len(words & terms(p)), i, p) for i,p in enumerate(sections)), key=lambda item: (-item[0],item[1]))
    result = [p for score, _, p in matches[:3] if score]
    return "\n\n".join(result) if result else "Nenhum trecho corresponde à consulta na base deste agente. Tente um termo mais específico."
