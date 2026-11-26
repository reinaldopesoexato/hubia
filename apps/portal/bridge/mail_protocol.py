"""IMAP and SMTP access to the institutional Skymail profile."""
import imaplib
import re
import smtplib
import ssl
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html import escape
from html.parser import HTMLParser

from .signature import signature_parts

IMAP_HOST = "imap.emailemnuvem.com.br"
SMTP_HOST = "smtp.emailemnuvem.com.br"
PAGE = 10
MAX_BYTES = 256 * 1024
SEARCH_FIELDS = {"remetente":"FROM", "assunto":"SUBJECT", "texto":"TEXT"}


@contextmanager
def inbox(address, password):
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=ssl.create_default_context(), timeout=8) as client:
            if b"AUTH=PLAIN" in client.capabilities:
                client.authenticate("PLAIN", lambda _: b"\0" + address.encode() + b"\0" + password.encode())
            else:
                client.login(address, password)
            status, count = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ValueError("O provedor recusou a abertura da caixa de entrada.")
            yield client, int(count[0])
    except (imaplib.IMAP4.error, OSError, EOFError, UnicodeError):
        raise ValueError("Falha ao acessar o IMAP. Verifique o endereço, a senha e a habilitação do IMAP no provedor.") from None


def number(client, key):
    _, data = client.response(key)
    if not data or not data[0] or not data[0].isdigit():
        raise ValueError("O servidor não forneceu o identificador da caixa.")
    return int(data[0])


def literal(response):
    status, parts = response
    if status != "OK":
        raise ValueError("O servidor recusou a leitura. Tente novamente com o mesmo marcador.")
    for part in parts:
        if isinstance(part, tuple) and isinstance(part[1], bytes):
            return part[1]
    return None


def page(client, count, latest, after):
    if after is None and count:
        status, rows = client.fetch(f"{max(1, count - PAGE + 1)}:{count}", "(UID)")
        if status != "OK":
            raise ValueError("Falha ao listar a caixa de entrada.")
        uids = [int(match[1]) for row in rows if isinstance(row, bytes)
                for match in [re.search(rb"\bUID (\d+)\b", row)] if match and int(match[1]) <= latest]
        end = latest
    elif after is not None and after < latest:
        end = min(latest, after + 1000)
        status, rows = client.uid("search", None, "UID", f"{after+1}:{end}")
        if status != "OK":
            raise ValueError("Falha ao consultar mensagens novas.")
        uids = sorted(int(uid) for uid in (rows[0] or b"").split() if after < int(uid) <= end)
        if len(uids) > PAGE:
            uids = uids[:PAGE]
            end = uids[-1]
    else:
        return [], latest
    return headers(client, uids), end


def headers(client, uids):
    messages = []
    for uid in uids:
        raw = literal(client.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]<0.8192>)"))
        if raw is not None:
            header = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
            messages.append((uid, {key:str(header.get(field, "")) for key, field in
                             (("de","From"), ("assunto","Subject"), ("data","Date"))}))
    return messages


def search_filter(args):
    field = args.get("campo", "texto")
    term = args["termo"].strip()
    if field not in SEARCH_FIELDS:
        raise ValueError("Escolha remetente, assunto ou texto.")
    if not term or len(term) > 200 or not term.isprintable():
        raise ValueError("Informe um termo de busca com 1 a 200 caracteres, em uma linha.")
    return field, term


def search_page(client, count, upper, field, term):
    if not count or upper < 1:
        return [], None
    # Keep the SEARCH response within imaplib's line limit even in large mailboxes.
    # For smaller inboxes, search the full history even when its UIDs are sparse.
    lower = max(1, upper - 49999) if count > 50000 else 1
    charset = () if term.isascii() else ("CHARSET", "UTF-8")
    # imaplib transmits this last argument as a length-prefixed IMAP literal.
    client.literal = term.encode("utf-8")
    status, rows = client.uid("search", *charset, "UID", f"{lower}:{upper}", SEARCH_FIELDS[field])
    if status != "OK":
        raise ValueError("O servidor recusou a pesquisa na caixa de entrada.")
    uids = sorted((int(uid) for uid in (rows[0] or b"").split()), reverse=True)
    page_uids = uids[:PAGE]
    next_upper = page_uids[-1] - 1 if len(uids) > PAGE else lower - 1
    return headers(client, page_uids), next_upper or None


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style","script"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("style","script"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def read(client, uid):
    raw = literal(client.uid("fetch", str(uid), f"(BODY.PEEK[]<0.{MAX_BYTES+1}>)"))
    if raw is None:
        raise ValueError("Esta mensagem já não está na caixa de entrada.")
    msg = BytesParser(policy=policy.default).parsebytes(raw[:MAX_BYTES])
    part = msg.get_body(preferencelist=("plain", "html"))
    content = ""
    if part:
        data = part.get_payload(decode=True) or b""
        try:
            content = data.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            content = data.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            parser = HTMLText()
            parser.feed(content)
            content = " ".join(parser.parts)
    return {"de":str(msg.get("From", "")), "assunto":str(msg.get("Subject", "")),
            "texto":content[:16000], "truncado":len(raw)>MAX_BYTES or len(content)>16000}


def submit(address, password, recipient, subject, body, signature_html=""):
    message = EmailMessage()
    message["From"] = address
    message["To"] = recipient
    message["Subject"] = subject
    if signature_html:
        html, plain = signature_parts(signature_html)
        message.set_content(f"{body}\n\n{plain}")
        html_body = "<br>\n".join(escape(body).splitlines())
        message.add_alternative(f"<div>{html_body}</div><br><div>{html}</div>", subtype="html")
    else:
        message.set_content(body)
    accepted = False
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(), timeout=10) as client:
            client.login(address, password)
            client.send_message(message, from_addr=address, to_addrs=[recipient])
            accepted = True
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException, smtplib.SMTPNotSupportedError):
        return "aceito_pelo_smtp" if accepted else "recusado"
    except (OSError, smtplib.SMTPException, UnicodeError):
        return "aceito_pelo_smtp" if accepted else "incerto"
    return "aceito_pelo_smtp"
