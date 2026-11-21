"""A fake website to crawl, served on localhost.

Testing a crawler against the live web is the wrong idea twice over: the
results change under you, and it is rude. So the test suite serves its own
site, with every hazard a crawler meets built in on purpose:

* **cycles** -- pages linking back to each other, so a crawler without a
  visited-set never terminates
* **duplicate content** -- the same article at two URLs
* **near-duplicate content** -- the same article with one sentence changed,
  which exact hashing cannot catch
* **a parameter trap** -- ``/calendar?day=N`` for any N, an infinite URL space
  from a finite site, which is how real crawlers die
* **errors** -- a 404, a 500, and a redirect chain
* **a slow page** -- to prove the timeout works
* **robots.txt** -- with a disallowed section that must never be fetched
* **``noindex`` and ``nofollow``** -- metadata a polite crawler honours
* **off-site and binary links** -- which the filters should reject

Because the site is generated from a seed, the whole suite is deterministic:
the same crawl produces the same corpus every time.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

__all__ = ["SitePage", "SiteServer", "build_site"]

WORDS = [
    "search",
    "index",
    "crawler",
    "frontier",
    "queue",
    "hash",
    "vector",
    "corpus",
    "parser",
    "token",
    "document",
    "retrieval",
    "latency",
    "throughput",
    "cluster",
    "memory",
    "disk",
    "cache",
    "network",
    "server",
    "protocol",
    "request",
    "response",
    "header",
    "payload",
    "schema",
    "pipeline",
    "worker",
    "scheduler",
]


@dataclass
class SitePage:
    """One response the fake site can serve."""

    body: str
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    delay: float = 0.0
    location: str | None = None


def _paragraph(rng: random.Random, words: int = 60) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words)).capitalize() + "."


def _article(rng: random.Random, sentences: int = 13, words: int = 25) -> str:
    """A page-length body.

    Real articles run to hundreds of words, and the length matters for more
    than realism: SimHash near-duplicate detection is length-sensitive, so a
    fixture of 70-word stubs would test it under conditions it is not meant
    for. See benchmarks/bench_simhash.py for the measurements.
    """
    return " ".join(_paragraph(rng, words) for _ in range(sentences))


def _page(title: str, body: str, links: list[str], extra_head: str = "") -> str:
    anchors = "\n".join(f'<li><a href="{href}">{href}</a></li>' for href in links)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><title>{title}</title>{extra_head}
<style>body {{ font-family: sans-serif }}</style>
<script>var tracking = 1;</script>
</head>
<body>
<nav>Home About Contact Privacy Terms Careers Blog Docs Support Status</nav>
<header>Site header repeated on every single page</header>
<main>
<h1>{title}</h1>
<p>{body}</p>
<ul>{anchors}</ul>
</main>
<footer>Copyright notice repeated on every single page</footer>
</body></html>"""


def build_site(
    articles: int = 12,
    seed: int = 0,
    slow_delay: float = 2.0,
) -> dict[str, SitePage]:
    """Generate the fake site as a mapping of path to response."""
    rng = random.Random(seed)
    pages: dict[str, SitePage] = {}

    article_paths = [f"/articles/{i}" for i in range(articles)]
    bodies = {path: _article(rng) for path in article_paths}

    # The front page links to everything, including the hazards.
    pages["/"] = SitePage(
        _page(
            "Index",
            _paragraph(rng, 40),
            [
                *article_paths[:6],
                "/section/",
                "/duplicate-of-0",
                "/near-duplicate-of-0",
                "/missing",
                "/broken",
                "/redirect/start",
                "/slow",
                "/noindex",
                "/nofollow-source",
                "/private/secret",
                "/calendar?day=1",
                "/report.pdf",
                "https://external.example.com/elsewhere",
                "mailto:someone@example.com",
                "/#fragment-only",
            ],
        )
    )

    # A second hub, so there is more than one route to later articles: this is
    # what makes duplicate-URL detection do real work.
    pages["/section/"] = SitePage(
        _page("Section", _paragraph(rng, 40), [*article_paths, "/", "/section/index.html"])
    )
    pages["/section/index.html"] = SitePage(_page("Section", _paragraph(rng, 40), ["/section/"]))

    for index, path in enumerate(article_paths):
        neighbours = [article_paths[(index + 1) % articles], "/", "/section/"]
        pages[path] = SitePage(_page(f"Article {index}", bodies[path], neighbours))

    # Byte-identical to article 0 at a different URL -- including its links,
    # because the anchor text is part of the extracted text and a different
    # link list would make these genuinely different pages.
    article_zero_links = [article_paths[1 % articles], "/", "/section/"]
    pages["/duplicate-of-0"] = SitePage(
        _page("Article 0", bodies["/articles/0"], article_zero_links)
    )
    # Same article, one sentence added: exact hashing misses this, SimHash
    # catches it.
    pages["/near-duplicate-of-0"] = SitePage(
        _page(
            "Article 0",
            bodies["/articles/0"] + " One extra sentence appended to the end.",
            article_zero_links,
        )
    )

    pages["/missing"] = SitePage("<h1>Not found</h1>", status=404)
    pages["/broken"] = SitePage("<h1>Server error</h1>", status=500)
    pages["/redirect/start"] = SitePage("", status=302, location="/redirect/middle")
    pages["/redirect/middle"] = SitePage("", status=302, location="/redirect/end")
    pages["/redirect/end"] = SitePage(_page("Arrived", _paragraph(rng, 50), ["/"]))
    pages["/slow"] = SitePage(_page("Slow", _paragraph(rng, 50), ["/"]), delay=slow_delay)
    pages["/noindex"] = SitePage(
        _page(
            "Noindex",
            _paragraph(rng, 50),
            ["/articles/0", "/only-reachable-from-noindex"],
            extra_head='<meta name="robots" content="noindex">',
        )
    )
    # Reachable only through the noindex page, which proves links are followed
    # from pages that are not themselves stored.
    pages["/only-reachable-from-noindex"] = SitePage(
        _page("Hidden gem", _paragraph(rng, 50), ["/"])
    )
    pages["/nofollow-source"] = SitePage(
        f"""<!DOCTYPE html><html><head><title>Nofollow</title></head><body><main>
        <p>{_paragraph(rng, 40)}</p>
        <a href="/never-followed" rel="nofollow">do not follow</a>
        <a href="/">home</a></main></body></html>"""
    )
    pages["/never-followed"] = SitePage(_page("Never", _paragraph(rng, 50), ["/"]))
    pages["/private/secret"] = SitePage(_page("Secret", "This must never be fetched.", ["/"]))
    pages["/report.pdf"] = SitePage("%PDF-1.4 binary", content_type="application/pdf")
    pages["/tiny"] = SitePage(_page("Tiny", "Four words only here.", ["/"]))

    # Fails the first `flaky_failures` times, then succeeds: the only honest
    # way to test that retries actually recover rather than just repeat.
    pages["/flaky"] = SitePage(_page("Flaky", _article(rng), ["/"]))
    # Larger than any sane max_bytes, to prove the download is abandoned.
    pages["/huge"] = SitePage(_page("Huge", "padding " * 400_000, ["/"]))

    pages["/robots.txt"] = SitePage(
        "User-agent: *\nDisallow: /private/\nAllow: /\nSitemap: /sitemap.xml\n",
        content_type="text/plain; charset=utf-8",
    )
    return pages


class _Handler(BaseHTTPRequestHandler):
    server_version = "nanocrawl-testsite/1.0"
    pages: dict[str, SitePage] = {}
    hits: dict[str, int] = {}
    # (monotonic time, path) per request, so tests can assert that the
    # politeness delay was actually honoured on the wire rather than trusting
    # the crawler's own accounting.
    timeline: list[tuple[float, str]] = []
    flaky_failures: int = 1
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming
        parts = urlsplit(self.path)
        path = parts.path

        with self.lock:
            self.hits[self.path] = self.hits.get(self.path, 0) + 1
            self.timeline.append((time.monotonic(), self.path))

        # The parameter trap: every ?day= value is a "new" page that links to
        # the next one, forever. A crawler needs a depth or budget limit to
        # survive it -- no amount of URL deduplication helps, because every
        # URL really is different.
        if path == "/calendar":
            day = parts.query.removeprefix("day=") or "1"
            try:
                number = int(day)
            except ValueError:
                number = 1
            body = _page(
                f"Day {number}",
                "A calendar page for one day. " * 12,
                [f"/calendar?day={number + 1}", f"/calendar?day={number + 2}", "/"],
            )
            self._respond(SitePage(body))
            return

        if path == "/flaky":
            with self.lock:
                attempts = self.hits.get(self.path, 0)
            if attempts <= self.flaky_failures:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        page = self.pages.get(path)
        if page is None:
            self._respond(SitePage("<h1>Not found</h1>", status=404))
            return
        self._respond(page)

    def _respond(self, page: SitePage) -> None:
        if page.delay:
            time.sleep(page.delay)
        body = page.body.encode("utf-8")
        self.send_response(page.status)
        if page.location:
            self.send_header("Location", page.location)
        self.send_header("Content-Type", page.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # noqa: ARG002 - stdlib hook
        return  # silence; the crawler's own stats are the signal

    def handle_one_request(self) -> None:
        # A client that times out or abandons an oversized body drops the
        # connection mid-write. That is the behaviour under test, not an
        # error, so it must not print a traceback.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class _QuietServer(ThreadingHTTPServer):
    """A server that does not shout when a client hangs up mid-response."""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        import sys

        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)  # type: ignore[arg-type]


@dataclass
class SiteServer:
    """Serves :func:`build_site` on a free localhost port.

    Used as a context manager::

        with SiteServer() as site:
            await crawl(CrawlConfig(seeds=[site.url("/")]))
    """

    pages: dict[str, SitePage] = field(default_factory=build_site)
    host: str = "127.0.0.1"
    flaky_failures: int = 1
    _server: _QuietServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def start(self) -> SiteServer:
        handler = type(
            "_BoundHandler",
            (_Handler,),
            {
                "pages": self.pages,
                "hits": {},
                "timeline": [],
                "lock": threading.Lock(),
                "flaky_failures": self.flaky_failures,
            },
        )
        self._server = _QuietServer((self.host, 0), handler)  # type: ignore[arg-type]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not running")
        return int(self._server.server_address[1])

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str = "/") -> str:
        return f"{self.origin}{path}"

    @property
    def hits(self) -> dict[str, int]:
        """How many times each path was requested."""
        if self._server is None:
            return {}
        return dict(self._server.RequestHandlerClass.hits)  # type: ignore[attr-defined]

    @property
    def timeline(self) -> list[tuple[float, str]]:
        """Every request, in order, with the time it arrived."""
        if self._server is None:
            return []
        return list(self._server.RequestHandlerClass.timeline)  # type: ignore[attr-defined]

    def gaps(self) -> list[float]:
        """Seconds between consecutive requests, for politeness assertions."""
        times = [moment for moment, _ in self.timeline]
        return [b - a for a, b in zip(times, times[1:], strict=False)]

    def __enter__(self) -> SiteServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
