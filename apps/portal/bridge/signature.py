"""Restricted HTML shared by the signature editor, MCP and SMTP."""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_LENGTH = 6000
BLOCKS = {"div", "p", "li", "ul", "ol"}
TAGS = BLOCKS | {"br", "strong", "b", "em", "i", "u", "a", "span"}


class SignatureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html = []
        self.text = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            raise ValueError(f"A tag <{tag}> não é aceita na assinatura.")
        href = ""
        for name, value in attrs:
            if tag != "a" or name != "href" or href or not value:
                raise ValueError("Use apenas formatação simples e o atributo href nos links.")
            url = urlsplit(value)
            if (any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value
                    or not ((url.scheme == "https" and url.hostname)
                            or (url.scheme in {"mailto", "tel"} and url.path))):
                raise ValueError("Use links https://, mailto: ou tel: na assinatura.")
            href = value
        attr = f' href="{escape(href, quote=True)}"' if href else ""
        self.html.append(f"<{tag}{attr}>")
        if tag == "br":
            self.text.append("\n")
        else:
            if tag in BLOCKS and self.text and not self.text[-1].endswith("\n"):
                self.text.append("\n")
            self.stack.append((tag, href))

    def close_tag(self):
        tag, href = self.stack.pop()
        self.html.append(f"</{tag}>")
        if href:
            self.text.append(f" ({href})")
        if tag in BLOCKS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("Confira a abertura e o fechamento das tags HTML.")
        self.close_tag()

    def handle_data(self, data):
        self.html.append(escape(data, quote=False))
        self.text.append(data)


def signature_parts(source):
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if len(source) > MAX_LENGTH or any(ord(c) < 32 and c not in "\n\t\r" for c in source):
        raise ValueError("A assinatura deve ter até 6.000 caracteres de HTML válido.")
    try:
        source.encode("utf-8")
        parser = SignatureParser()
        parser.feed(source)
        parser.close()
        while parser.stack:
            parser.close_tag()
    except (AssertionError, UnicodeError):
        raise ValueError("Confira o código HTML da assinatura.") from None
    html = "".join(parser.html)
    if len(html) > MAX_LENGTH:
        raise ValueError("A assinatura formatada ultrapassa 6.000 caracteres.")
    plain = "".join(parser.text).strip()
    return (html, plain) if plain else ("", "")
