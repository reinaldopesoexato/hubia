"""Client for the isolated printer of a native AviHUB preview."""
import os
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urlsplit


HTML_LIMIT = 8 * 1024 * 1024
PDF_LIMIT = 24 * 1024 * 1024
TIMEOUT = 25


class PDFRenderError(ValueError):
    """The Hub does not have a confirmed PDF from the native preview."""


def valid_pdf(data):
    return data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")


def render_pdf(html: str) -> bytes:
    try:
        endpoint = urlsplit(os.environ.get("HUBIA_PDF_RENDERER", ""))
        port = endpoint.port
    except ValueError:
        raise PDFRenderError("Impressão da prévia do AviHUB indisponível.") from None
    if (endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username is not None
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise PDFRenderError("Impressão da prévia do AviHUB indisponível.")

    payload = html.encode("utf-8")
    if not payload or len(payload) > HTML_LIMIT:
        raise PDFRenderError("A prévia do AviHUB excede o limite de impressão ou está vazia.")

    client = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
    conn = client(endpoint.hostname, port, timeout=TIMEOUT)
    try:
        conn.request("POST", "/render", payload, {"Content-Type":"text/html; charset=utf-8"})
        with conn.getresponse() as response:
            if response.status != 200 or response.headers.get_content_type() != "application/pdf":
                raise PDFRenderError("A impressão da prévia do AviHUB não foi concluída.")
            data = response.read(PDF_LIMIT + 1)
    except (OSError, HTTPException):
        raise PDFRenderError("A impressão da prévia do AviHUB não foi concluída.") from None
    finally:
        conn.close()
    if len(data) > PDF_LIMIT or not valid_pdf(data):
        raise PDFRenderError("A impressão da prévia do AviHUB não retornou um PDF válido.")
    return data
