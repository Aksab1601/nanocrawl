"""Pulling links and readable text out of HTML, with the standard library.

``html.parser.HTMLParser`` is an event-driven parser: it calls a method as it
meets each tag. Subclassing it is about 120 lines here and avoids a dependency
on BeautifulSoup or lxml, which keeps the whole project at one third-party
package.

Two jobs:

**Links**, so the crawl can continue. Every ``href`` on an ``<a>``, resolved
against the page's URL (or its ``<base href>`` if it declares one).

**Text**, so the output is a corpus rather than a pile of markup. This is the
part people underestimate. A raw HTML-to-text conversion gives you the
navigation menu, the cookie banner and the footer on *every single page* --
which is worse than useless for a search index, because those shared blocks
make every page look similar to every other one. So:

* ``<script>``, ``<style>``, ``<noscript>``, ``<template>`` and ``<svg>``
  contents are dropped entirely
* ``<nav>``, ``<header>``, ``<footer>`` and ``<aside>`` are dropped by default
  as boilerplate
* if the page has a ``<main>`` or ``<article>``, only that is kept -- a page
  that says where its content is should be believed

The parser is also tolerant by design: real HTML is malformed constantly, and
a crawler that raises on a stray ``</div>`` will not survive its first page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = ["ParsedPage", "parse_html"]

# Contents never contribute text.
INVISIBLE = frozenset({"script", "style", "noscript", "template", "svg", "head"})
# Contents are usually repeated site furniture.
BOILERPLATE = frozenset({"nav", "header", "footer", "aside", "form"})
# Tags whose boundaries are paragraph breaks in the extracted text.
BLOCK = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    ]
)
MAIN_CONTENT = frozenset({"main", "article"})

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_ANY_SPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class ParsedPage:
    """What one HTML document yielded."""

    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    base_href: str | None = None
    language: str | None = None
    noindex: bool = False
    nofollow: bool = False
    description: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class _PageParser(HTMLParser):
    """Collects links and readable text in a single pass."""

    def __init__(self, drop_boilerplate: bool = True) -> None:
        # convert_charrefs turns &amp; into & for us.
        super().__init__(convert_charrefs=True)
        self.drop_boilerplate = drop_boilerplate

        self.page = ParsedPage()
        self._chunks: list[str] = []
        self._main_chunks: list[str] = []

        self._invisible_depth = 0
        self._boilerplate_depth = 0
        self._main_depth = 0
        self._in_title = False
        self._saw_main = False

    # ------------------------------------------------------------ collecting

    def _emit(self, text: str) -> None:
        self._chunks.append(text)
        if self._main_depth:
            self._main_chunks.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}

        if tag in INVISIBLE:
            self._invisible_depth += 1
            return
        if self.drop_boilerplate and tag in BOILERPLATE:
            self._boilerplate_depth += 1
        if tag in MAIN_CONTENT:
            self._main_depth += 1
            self._saw_main = True

        if tag == "title":
            self._in_title = True
        elif tag == "base" and "href" in attributes:
            self.page.base_href = attributes["href"].strip() or None
        elif tag == "html" and "lang" in attributes:
            self.page.language = attributes["lang"].strip() or None
        elif tag == "meta":
            self._handle_meta(attributes)
        elif tag == "a":
            self._handle_anchor(attributes)

        if tag in BLOCK:
            self._emit("\n")

    def _handle_meta(self, attributes: dict[str, str]) -> None:
        name = attributes.get("name", "").lower()
        content = attributes.get("content", "")
        if name == "robots":
            directives = {part.strip().lower() for part in content.split(",")}
            # A page may ask not to be indexed, or not to have its links
            # followed. Both are requests a polite crawler honours.
            self.page.noindex = "noindex" in directives or "none" in directives
            self.page.nofollow = "nofollow" in directives or "none" in directives
        elif name == "description":
            self.page.description = content.strip()

    def _handle_anchor(self, attributes: dict[str, str]) -> None:
        href = attributes.get("href", "").strip()
        if not href:
            return
        rel = {part.strip().lower() for part in attributes.get("rel", "").split()}
        if "nofollow" in rel:
            return
        self.page.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in INVISIBLE:
            self._invisible_depth = max(0, self._invisible_depth - 1)
            return
        if self.drop_boilerplate and tag in BOILERPLATE:
            self._boilerplate_depth = max(0, self._boilerplate_depth - 1)
        if tag in MAIN_CONTENT:
            self._main_depth = max(0, self._main_depth - 1)
        if tag == "title":
            self._in_title = False
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.page.title += data
            return
        if self._invisible_depth or self._boilerplate_depth:
            return
        # Source newlines inside a paragraph are just whitespace; only block
        # boundaries break a line. Whitespace-only runs still emit one space,
        # or "<b>a</b> <i>b</i>" would come out as "ab".
        collapsed = _ANY_SPACE.sub(" ", data)
        if collapsed.strip():
            self._emit(collapsed)
        elif collapsed:
            self._emit(" ")

    # -------------------------------------------------------------- finalise

    def finish(self) -> ParsedPage:
        # Prefer <main>/<article> when the page declared one, but fall back if
        # it turned out to be nearly empty -- some sites wrap a sidebar in
        # <main> and put the real content beside it.
        chunks = self._chunks
        if self._saw_main and self.drop_boilerplate:
            main_text = _tidy("".join(self._main_chunks))
            if len(main_text.split()) >= 20:
                self.page.text = main_text
                self.page.title = " ".join(self.page.title.split())
                return self.page

        self.page.text = _tidy("".join(chunks))
        self.page.title = " ".join(self.page.title.split())
        return self.page

    # Never let malformed markup end a crawl.
    def error(self, message: str) -> None:  # noqa: ARG002 - stdlib hook signature
        return


def _tidy(text: str) -> str:
    """Collapse runs of spaces and blank lines into readable paragraphs."""
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    joined = "\n".join(line for line in lines if line)
    return _BLANK_LINES.sub("\n\n", joined).strip()


def parse_html(markup: str, drop_boilerplate: bool = True) -> ParsedPage:
    """Parse one HTML document into text, links and metadata."""
    parser = _PageParser(drop_boilerplate=drop_boilerplate)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not kill the crawl
        pass
    return parser.finish()
