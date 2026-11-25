"""The words of an HTML page as plain lines (issue #45).

Loaded by ``material_extract.py`` inside its child process and nowhere else,
so it imports nothing but the standard library. The page is untrusted: it is
only read as text. Nothing it refers to is fetched (images, frames, style
sheets, scripts) and nothing in it is run; what a script would have written
into the page is simply not there.

What comes out is close to Markdown, because the knowledge service is given it
as a Markdown file: a line per block, ``#`` in front of headings, ``- `` in
front of list items (indented by their level), a table row on one line with
its cells joined by `` | `` (whatever the cells contain), a ``<pre>`` between
code fences with its lines as they are (commands stay whole), and the address
of a link to another web site in brackets after its words.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Their content is not text of the page.
SKIPPED = frozenset(("script", "style", "template", "noscript", "svg", "math", "iframe", "object", "select",
                     "textarea", "canvas", "audio", "video"))
HEADINGS = {f"h{level}": "#" * level + " " for level in range(1, 7)}
LISTS = frozenset(("ul", "ol"))
BLOCKS = frozenset(("p", "div", "section", "article", "header", "footer", "nav", "aside", "main", "ul", "ol", "dl",
                    "dt", "dd", "caption", "blockquote", "figure", "figcaption", "form", "fieldset", "address",
                    "details", "summary", "pre", "li", "hr", "br", "body", "html", "head", "title", *HEADINGS))
CELLS = frozenset(("td", "th"))
FENCE = "```"
TITLE_CHARS = 300
URL_CHARS = 2000
_SPACE = re.compile(r"\s+")
_LINE_END = re.compile(r"\r\n|\r|\n")
# Brackets are left out as well: an address is written inside brackets and must not be able to close them.
_WEB_ADDRESS = re.compile(r"https?://[^\s<>\"'`\\()\[\]]+\Z", re.IGNORECASE)
# Control characters (a character reference can name any of them) and the characters that turn the direction
# of the text around are dropped; the ones that separate words or lines count as a space. Tab and line feed stay.
_NO_CONTROLS = {**{code: None for code in (*range(9), *range(14, 32), *range(127, 160), *range(0x202A, 0x202F),
                                           *range(0x2066, 0x206A))},
                **{code: " " for code in (11, 12, 13, 0x2028, 0x2029)}}


class _Full(Exception):
    """The rest of the page is not read: the text budget is used up, or what follows is not text."""


class PageText(HTMLParser):
    # Newer versions of the standard library read these two as raw text up to their end tag, so a title
    # that is never closed would swallow the page. Here they are elements like any other, on every version.
    RCDATA_CONTENT_ELEMENTS = ()

    def __init__(self, budget: int):
        super().__init__(convert_charrefs=True)
        self.budget, self.used, self.truncated = budget, 0, False
        self.lines: list[str] = []
        self.line: list[str] = []
        self.pending = 0                                # characters collected for the line that is not ended yet
        self.prefix = ""
        self.skipping: list[str] = []
        self.pre = 0
        self.lists = 0
        self.tables: list[dict] = []                    # one per open table: cells of the current row, inside a cell?
        self.href, self.link_from, self.in_link = "", 0, False
        self.title, self.in_title = "", False

    # ------------------------------------------------------------ lines
    def _add(self, text: str) -> None:
        size = len(text.encode("utf-8")) + 1
        if self.used + size > self.budget:
            room = self.budget - self.used - 1
            cut = text.encode("utf-8")[:max(room, 0)].decode("utf-8", "ignore") if room > 0 else ""
            if cut:
                self.lines.append(cut)
            self.truncated = True
            raise _Full
        self.used += size
        self.lines.append(text)

    def _in_cell(self) -> bool:
        return any(table["in_cell"] for table in self.tables)

    def _break(self, hard: bool = False) -> None:
        """End the line that is being collected. Inside a table cell the row goes on: there it is a space."""
        if not hard and self._in_cell():
            self.line.append(" ")
            return
        joined = "".join(self.line).translate(_NO_CONTROLS)
        text = joined.rstrip() if self.pre and not self.tables else _SPACE.sub(" ", joined).strip()
        self.line, self.link_from, self.pending = [], 0, 0
        if not text:
            if self.pre and self.lines and self.lines[-1] not in ("", FENCE):
                self._add("")                           # an empty line inside a <pre> is kept, once
            return
        if self.prefix.startswith("#") and self.lines and self.lines[-1]:
            self._add("")
        prefix, self.prefix = self.prefix, ""
        self._add(prefix + text)

    def _table(self) -> dict:
        if not self.tables:                             # a row or a cell without its <table>
            self.tables.append({"cells": 0, "in_cell": False})
        return self.tables[-1]

    # ------------------------------------------------------------ the parser's calls
    def handle_starttag(self, tag, attrs):
        if self.skipping:
            if tag == self.skipping[-1]:
                self.skipping.append(tag)
            return
        self.in_title = False                           # a title that is never closed ends at the next element
        if tag in SKIPPED:
            self.skipping.append(tag)
            return
        soft = self._in_cell()
        if tag in BLOCKS:
            self._break()
        if tag == "title" and not self.title:
            self.in_title = True
        elif tag == "table":
            self._break()
            self.tables.append({"cells": 0, "in_cell": False})
        elif tag == "tr":
            self._break()
            self._table().update(cells=0, in_cell=False)
        elif tag in CELLS:
            table = self._table()
            if table["cells"]:
                self.line.append(" | ")
            table["cells"] += 1
            table["in_cell"] = True
        elif tag in LISTS:
            self.lists += 1
        elif soft:
            pass                                        # inside a cell nothing starts a line of its own
        elif tag in HEADINGS:
            self.prefix = HEADINGS[tag]
        elif tag == "li":
            self.prefix = "  " * max(self.lists - 1, 0) + "- "
        elif tag == "pre":
            self._add(FENCE)
            self.pre += 1
        if tag == "a":
            target = next((value for name, value in attrs if name == "href" and value), "").strip()
            self.href = target if len(target) <= URL_CHARS and _WEB_ADDRESS.match(target) else ""
            self.link_from, self.in_link = len(self.line), True

    def handle_endtag(self, tag):
        if self.skipping:
            if tag == self.skipping[-1]:
                self.skipping.pop()
            return
        if tag == "a" and self.in_link:                 # an end tag without its start has nothing to add
            if self.href:
                words = _SPACE.sub(" ", "".join(self.line[self.link_from:])).strip()
                if words != self.href:
                    self.line.append(f" ({self.href})" if words else f" {self.href} ")
            self.href, self.in_link = "", False
        if tag == "title":
            self.in_title = False
        if tag in CELLS:
            if self.tables:
                self.tables[-1]["in_cell"] = False
        elif tag == "tr":
            if self.tables:
                self.tables[-1].update(cells=0, in_cell=False)
            self._break()
        elif tag == "table":
            if self.tables:
                self.tables.pop()
            self._break()
        elif tag in LISTS:
            self.lists = max(self.lists - 1, 0)
        if tag in BLOCKS:
            self._break()
        if tag == "pre" and self.pre:
            self.pre -= 1
            if not self.pre:
                if self.lines and not self.lines[-1]:
                    self.lines.pop()                    # no empty line in front of the closing fence
                    self.used -= 1
                self._add(FENCE)
        elif tag in HEADINGS or tag == "li":
            self.prefix = ""

    def handle_data(self, data):
        if self.skipping:
            return
        if self.in_title:
            self.title = (self.title + data)[:TITLE_CHARS]
            return
        if data.startswith("<!--") and "-->" not in data:
            # A comment that is never closed hides the rest of the page; older versions of the standard
            # library hand it over as text and read on. Stop, as the newer ones do.
            self._break(hard=True)
            raise _Full
        self.pending += len(data)
        if self.pending > self.budget:                  # a line that never ends is cut like any other text
            self.line.append(data)
            self._break(hard=True)
            self.truncated = True
            raise _Full
        if not self.pre:
            self.line.append(data)
            return
        first, *rest = _LINE_END.split(data)
        self.line.append(first)
        for part in rest:
            self._break()
            self.line.append(part)


def page_text(page: str, budget: int) -> dict:
    """``{"text", "title", "truncated"}`` of the page; at most ``budget`` bytes of text (UTF-8).
    A page without words in its body has no text, whatever its title says."""
    reader = PageText(budget)
    try:
        reader.feed(page)
        reader.close()
        reader._break(hard=True)
    except _Full:
        pass
    lines = reader.lines
    while lines and not lines[-1]:
        lines.pop()
    if lines and lines[-1] == FENCE and lines.count(FENCE) % 2:
        lines.pop()                                     # a fence that opens nothing (the page was cut there)
    title = _SPACE.sub(" ", reader.title.translate(_NO_CONTROLS)).strip()
    heading = "# " + title
    if title and lines and heading not in lines[:3]:
        lines.insert(0, heading)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > budget:               # the title line came on top of a full budget
        text = text.encode("utf-8")[:budget].decode("utf-8", "ignore")
        reader.truncated = True
    return {"text": text, "title": title, "truncated": reader.truncated}
