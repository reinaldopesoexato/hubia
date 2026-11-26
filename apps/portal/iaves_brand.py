"""IAves brand spelling for display text and Markdown."""

import re


NAME = "IAves"
NAME_MARKDOWN = "**IA**ves"
DISPLAY_INSTRUCTIONS = (f"Para apresentar a marca ao usuário, escreva {NAME} em texto simples. "
                        f"Em Markdown, use {NAME_MARKDOWN} para destacar apenas IA em negrito.")

LITERAL_TEXT = re.compile(r"(```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]*`|"
                          r"(?:https?://|www\.)[^\s<>\[\]()]+|\]\([^\n)]*\))", re.I)
BRAND = re.compile(r"(?<![\w/.])(?:\*\*ia\*\*ves|__ia__ves|\*\*iaves\*\*|__iaves__|iaves)"
                   r"(?![\w/]|\.\w)", re.I)


def display_name(value, *, markdown=False):
    """Normalize the agent name in prose while preserving code, links and paths."""
    replacement = NAME_MARKDOWN if markdown else NAME
    return "".join(piece if index % 2 else BRAND.sub(replacement, piece)
                   for index, piece in enumerate(LITERAL_TEXT.split(value)))
