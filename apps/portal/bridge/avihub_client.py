"""Authenticate one AviHUB user and transport their native frontend requests."""
import json
import re
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from .avihub_api import EXPIRED, AccessDenied, SessionExpired
from .network import public_response, RemoteError

LOGIN = "/users/sign_in"
MFA = "/users/two_factor_auth"
PURPOSE = "Consulta equipamentos, pesagens, ração, silos e sensores da unidade autorizada pela sua conta AviHUB."


class Panel:
    def __init__(self, origin, cookies=None):
        self.origin = origin
        self.cookies = dict(cookies or {})

    def request(self, path, fields=None, *, accept="text/html", referer=None):
        status, location, _, body = self.exchange(path, fields, accept=accept, referer=referer)
        return status, urlsplit(location).path if location else None, body

    def exchange(self, path, fields=None, *, accept="text/html", referer=None, read_timeout=4, limit=8 * 1024 * 1024):
        headers = {"Accept": accept, "Accept-Encoding": "identity",
                   "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        if referer is not None:
            headers["Referer"] = self.origin + referer
        payload = None
        if fields is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": self.origin})
            payload = urlencode(fields, doseq=True).encode()
        try:
            status, response, body = public_response(self.origin + path, payload, headers,
                                                     limit=limit, read_timeout=read_timeout)
            for line in response.getlist("Set-Cookie"):
                jar = SimpleCookie()
                jar.load(line)
                for key, cookie in jar.items():
                    if cookie["path"] not in ("", "/"):
                        continue
                    if cookie.value and cookie["max-age"] != "0":
                        self.cookies[key] = cookie.coded_value
                    else:
                        self.cookies.pop(key, None)
        except (RemoteError, CookieError):
            raise ValueError("O AviHUB não respondeu com uma página válida. Tente novamente.") from None
        try:
            destination = urlsplit(urljoin(self.origin + path, response.get("Location", "")))
        except ValueError:
            raise ValueError("O AviHUB retornou um redirecionamento inválido.") from None
        location = urlunsplit(("", "", destination.path, destination.query, "")) if (
            destination.scheme + "://" + destination.netloc == self.origin and not destination.fragment
        ) else None
        return status, location, response, body

    def form(self, path, target=None):
        status, _, body = self.request(path)
        if status != 200:
            raise ValueError("A página de autenticação do AviHUB está indisponível.")
        doc = BeautifulSoup(body, "html.parser")
        for form in doc.select("form[action]"):
            if urlsplit(form["action"]).path.rstrip("/") == (target or path) and form.get("method", "").lower() == "post":
                token = form.select_one('input[name="authenticity_token"]')
                if token and token.get("value"):
                    return token["value"]
        raise ValueError("O AviHUB não apresentou o formulário de autenticação esperado.")

    def authenticate(self, email=None, password=None, code=None):
        path = MFA if code is not None else LOGIN
        fields = {"code": code} if code is not None else {"user[email]": email, "user[password]": password,
                                                         "user[remember_me]": "1"}
        fields["authenticity_token"] = self.form(path)
        status, location, _ = self.request(path, fields)
        if status in (302, 303) and (location or "").rstrip("/") == MFA and code is None:
            return False
        if status not in (302, 303) or not location or location.rstrip("/") in (MFA, LOGIN):
            raise ValueError("O AviHUB não aceitou o login ou o código de verificação.")
        self.home()
        return True

    def request_password_reset(self, email):
        path = "/users/password"
        form_path = path + "/new"
        token = self.form(form_path, path)
        status, location, body = self.request(path, {"authenticity_token": token, "user[email]": email},
                                              referer=form_path)
        if status in (302, 303) and location in (LOGIN, "/"):
            return
        # Devise can render the native form again for an unregistered address.
        if status in (200, 422) and BeautifulSoup(body, "html.parser").select_one(
                'form[action="/users/password"] input[name="user[email]"]'):
            return
        if status == 429:
            raise ValueError("O AviHUB recebeu muitos pedidos. Aguarde alguns minutos antes de tentar novamente.")
        raise ValueError("O AviHUB não confirmou o pedido de redefinição. Tente novamente mais tarde.")

    def send_code(self, channel):
        if channel not in ("sms", "whatsapp"):
            raise ValueError("Canal inválido.")
        status, _, body = self.request(MFA + "/send_otp", {
            "authenticity_token": self.form(MFA, MFA + "/send_otp"), "channel": channel}, accept="application/json")
        if status != 200 or not self.object(body).get("success"):
            raise ValueError("O AviHUB não pôde enviar o código por esse canal.")

    def home(self):
        # This ordinary signed-in layout includes the unit switcher for every role.
        return self.document("/notifications")

    def content(self, path, accept="text/html"):
        status, location, body = self.request(path, accept=accept)
        if status == 401 or (status in (301, 302, 303) and location in (LOGIN, MFA)):
            raise SessionExpired(EXPIRED)
        if status != 200:
            raise AccessDenied("A conta não tem acesso a esta página do AviHUB.")
        return body

    def document(self, path):
        doc = BeautifulSoup(self.content(path), "html.parser")
        if not doc.select_one('nav.navbar-body form[action="/users/sign_out"]'):
            raise AccessDenied("O AviHUB não confirmou o acesso ao painel.")
        return doc

    @staticmethod
    def unit_options(doc):
        options = doc.select('header form[action="/user_settings/switch_unit"] select[name="unit_id"] option')
        if not options or any(not re.fullmatch(r"[0-9]+", o.get("value", "")) for o in options):
            raise AccessDenied("O painel não confirmou as unidades disponíveis.")
        return options

    def units(self, doc=None):
        return [{"id": o["value"], "nome": o.get_text(strip=True), "ativa": o.has_attr("selected")}
                for o in self.unit_options(doc if doc is not None else self.home())]

    def require_unit(self, doc, unit_id):
        active = {o["value"] for o in self.unit_options(doc) if o.has_attr("selected")}
        if active != {unit_id}:
            raise AccessDenied(f"Ative a unidade {unit_id} no AviHUB com selecionar_unidade_avihub após a escolha do usuário.")

    def activate(self, unit):
        if not unit["ativa"]:
            path = "/user_settings/switch_unit?" + urlencode({"unit_id": unit["id"]})
            status, location, _ = self.request(path, referer="/notifications")
            if status in (302, 303) and location in (LOGIN, MFA):
                raise SessionExpired(EXPIRED)
            if status not in (302, 303) or location not in ("/notifications", "/"):
                raise AccessDenied("O AviHUB não confirmou a troca de unidade.")
        self.require_unit(self.home(), unit["id"])
        return {"unidade": {**unit, "ativa": True}}

    def page(self, path, params, unit_id, module):
        doc = self.document(path + ("?" + urlencode(params) if params else ""))
        self.require_context(doc, unit_id, module)
        return doc

    def require_context(self, doc, unit_id, module):
        self.require_unit(doc, unit_id)
        if module not in {a["href"] for a in doc.select("nav.navbar-body a[href]")}:
            raise AccessDenied("A conta não tem acesso ao módulo desta consulta na unidade.")

    def options(self, target, scope):
        path = "/load_children?" + urlencode({**scope, "targets": target})
        body = self.content(path, "text/vnd.turbo-stream.html")
        doc = BeautifulSoup(body, "html.parser")
        fragment = doc.find("turbo-stream", attrs={"target": target, "action": "update"})
        if fragment is None or fragment.template is None:
            raise AccessDenied("O AviHUB não confirmou as opções permitidas.")
        options = {}
        for option in fragment.template.find_all("option"):
            value = option.get("value", "")
            if value and re.fullmatch(r"[0-9]+", value):
                options[value] = "".join(str(node) for node in option.find_all(string=True)).strip()
        return options

    @staticmethod
    def object(body):
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                return value
        except (UnicodeError, ValueError):
            pass
        raise ValueError("O painel AviHUB retornou dados em formato inesperado.")
