from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from iaves_brand import BRAND, LITERAL_TEXT, NAME

register = template.Library()
HTML_NAME = format_html('<span class="iaves-brand"><b>{}</b>{}</span>', NAME[:2], NAME[2:])


@register.simple_tag
def iaves_brand(value):
    parts = (escape(piece) for piece in LITERAL_TEXT.split(str(value)))
    return mark_safe("".join(piece if index % 2 else BRAND.sub(HTML_NAME, piece)
                             for index, piece in enumerate(parts)))
