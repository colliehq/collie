"""Readable text from HTML someone else wrote, in bounded time.

Mail bodies are written by whoever sends the mail. The standard library's html.parser is quadratic
on some inputs before Python 3.13.4 / 3.12.11 (CVE-2025-6069), and the Windows runtime Collie ships
is 3.12.10: a few tens of kilobytes of "<a " there take seconds, a few hundred take minutes. This
reads markup with one linear pass instead -- comments and script/style/head/title blocks dropped,
tags turned into line breaks or nothing, entities decoded -- and stops at a size cap and a time
budget, saying whether it read everything so a caller can refuse rather than silently truncate.
"""
from __future__ import annotations

import html
import re
import time

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DROPPED_BLOCK = re.compile(r"<(script|style|head|title|noscript|template)(?=[\s/>]|$)")
_TOKEN = re.compile(r"<[^<>]*>|[^<]+|<")
_TAG_NAME = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)")
_BREAKS = {"br", "p", "div", "li", "tr", "table", "ul", "ol", "blockquote", "section", "article",
           "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr"}


def drop_invisible(markup):
    """Markup without comments and script/style/head/title blocks, in linear time."""
    low = markup.translate(_ASCII_LOWER)                 # same length as markup, unlike .lower()
    out, i, end = [], 0, len(markup)
    while i < end:
        j = markup.find("<", i)
        if j < 0:
            out.append(markup[i:])
            break
        out.append(markup[i:j])
        if low.startswith("<!--", j):
            k = low.find("-->", j + 4)
            i = end if k < 0 else k + 3
            continue
        block = _DROPPED_BLOCK.match(low, j)
        if block:
            k = low.find("</" + block.group(1), block.end())
            k = -1 if k < 0 else low.find(">", k)
            i = end if k < 0 else k + 1                  # unclosed: the rest is inside it
            continue
        out.append("<")
        i = j + 1
    return "".join(out)


def readable(markup, *, max_chars, budget):
    """``(text, complete)`` from ``markup``. ``complete`` is False when ``max_chars`` of visible
    markup or ``budget`` seconds stopped the reading early."""
    deadline = time.monotonic() + budget
    visible = drop_invisible(markup)
    complete = len(visible) <= max_chars
    parts = []
    for count, match in enumerate(_TOKEN.finditer(visible, 0, max_chars)):
        if count % 64 == 0 and time.monotonic() >= deadline:
            return "".join(parts), False
        token = match.group(0)
        if len(token) > 1 and token[0] == "<":
            name = _TAG_NAME.match(token)
            if name and name.group(1).lower() in _BREAKS:
                parts.append("\n")
            continue                                     # any other tag, doctype or CDATA marker
        parts.append(html.unescape(token))
    return "".join(parts), complete
