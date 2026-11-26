"""Isolated, single-request HTTP service that prints AviHUB's original HTML."""
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from .avihub_pdf import HTML_LIMIT, PDF_LIMIT, valid_pdf


RENDER_TIMEOUT = 20
SOCKET_TIMEOUT = 6
PORT = 8080


def print_html(html):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=8_000,
            executable_path=os.environ.get("HUBIA_CHROMIUM", "/usr/bin/chromium-headless-shell"))
        try:
            context = browser.new_context(offline=True, java_script_enabled=False,
                                          service_workers="block", accept_downloads=False)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content(html, wait_until="load", timeout=8_000)
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def print_request(data):
    # Run Chromium in its own process group so a timed-out print cannot outlive the request.
    with subprocess.Popen([sys.executable, "-m", __spec__.name, "--print"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
        try:
            pdf, _ = child.communicate(data, timeout=RENDER_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            return None
    return pdf if child.returncode == 0 and valid_pdf(pdf) else None


class RendererHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        super().setup()

    def log_message(self, *args):
        pass

    def respond(self, status, data=b"", mime="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, b"ok\n")
        else:
            self.respond(404)

    def do_POST(self):
        if self.path != "/render":
            self.respond(404)
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "text/html":
            self.respond(415)
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 8:
            self.respond(400)
            return
        length = int(lengths[0])
        if length > HTML_LIMIT:
            self.respond(413)
            return
        data = self.rfile.read(length)
        if not data or len(data) != length:
            self.respond(400)
            return
        pdf = print_request(data)
        if pdf is None:
            self.respond(422)
            return
        self.respond(200, pdf, "application/pdf")


class RendererServer(HTTPServer):
    def handle_error(self, request, client_address):
        print("Falha na solicitação de impressão.", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        body = sys.stdin.buffer.read(HTML_LIMIT + 1)
        if not body or len(body) > HTML_LIMIT:
            raise SystemExit(1)
        rendered = print_html(body.decode("utf-8"))
        if len(rendered) > PDF_LIMIT or not valid_pdf(rendered):
            raise SystemExit(1)
        sys.stdout.buffer.write(rendered)
    else:
        with RendererServer(("0.0.0.0", PORT), RendererHandler) as server:
            server.serve_forever()
