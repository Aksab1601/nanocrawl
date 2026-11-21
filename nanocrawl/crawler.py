"""The crawler: a pool of workers pulling from one frontier.

The shape of the thing
----------------------
``concurrency`` worker coroutines run the same loop forever: take a URL from
the frontier, check robots.txt, wait for this host's turn, fetch, parse, store,
queue the links found, repeat. They stop when the frontier says the crawl is
finished -- nothing queued *and* nothing in flight.

Why coroutines rather than threads: a crawler spends essentially all of its
time waiting for other people's servers. Threads would work, but each costs a
megabyte of stack to sit idle in, and with asyncio one thread supervises
hundreds of in-flight requests. The work is I/O-bound, so the GIL never enters
into it.

The order of operations inside a page matters, and is not obvious:

1. **robots.txt first.** Before anything is requested from the host.
2. **Politeness wait second.** Before the fetch, never after -- waiting after
   the request has already gone out protects nobody.
3. **Links queued before the page is filtered.** A page can be too short to
   store, or ask not to be indexed, and still be the only route to the rest of
   the site. Dropping its links would silently cut off whole sections.
4. **Storage last**, once the page has survived every filter.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .dedup import BloomFilter, ContentDeduplicator, UrlSet, content_hash
from .fetch import Fetcher
from .frontier import Frontier, Request
from .parse import ParsedPage, parse_html
from .robots import RobotsCache
from .stats import CrawlStats
from .store import Document, JsonlWriter
from .urls import UrlFilter, normalise

__all__ = ["CrawlConfig", "Crawler", "crawl"]

PageHook = Callable[[Document, CrawlStats], None]


@dataclass
class CrawlConfig:
    """Everything a crawl needs to know.

    The defaults are deliberately timid: one request every half second per
    host, 500 pages, three links deep, same site only. A crawler's defaults
    are a statement about who pays for a mistake, and it is not the person
    running it.
    """

    seeds: list[str]
    max_pages: int | None = 500
    max_depth: int = 3
    concurrency: int = 8
    delay: float = 0.5
    user_agent: str = "nanocrawl/0.1 (+https://github.com/)"
    timeout: float = 15.0
    max_retries: int = 2
    max_bytes: int = 2_000_000

    same_site_only: bool = True
    include_subdomains: bool = True
    allow_prefixes: tuple[str, ...] = ()
    deny_prefixes: tuple[str, ...] = ()
    strip_trailing_slash: bool = False

    drop_boilerplate: bool = True
    min_words: int = 20
    near_duplicates: bool = True
    near_duplicate_distance: int = 7

    use_bloom: bool = False
    bloom_capacity: int = 1_000_000
    bloom_error_rate: float = 0.01

    checkpoint_path: Path | None = None
    checkpoint_every: int = 100

    def frontier_budget(self) -> int | None:
        """Cap on URLs ever queued.

        A multiple of ``max_pages`` rather than ``max_pages`` itself: some
        queued URLs will be disallowed by robots, time out, or turn out to be
        PDFs, and stopping the queue at exactly the page budget would leave
        the crawl short. The multiple is what bounds memory.
        """
        return None if self.max_pages is None else max(self.max_pages * 5, 100)


@dataclass
class Crawler:
    """Runs one crawl."""

    config: CrawlConfig
    writer: JsonlWriter | None = None
    on_page: PageHook | None = None
    stats: CrawlStats = field(default_factory=CrawlStats)

    def __post_init__(self) -> None:
        seeds = [normalise(seed) for seed in self.config.seeds]
        self._seeds = [seed for seed in seeds if seed]
        if not self._seeds:
            raise ValueError("no usable seed URLs")

        self._seen = (
            BloomFilter(self.config.bloom_capacity, self.config.bloom_error_rate)
            if self.config.use_bloom
            else UrlSet()
        )
        self._frontier = Frontier(
            seen=self._seen,
            max_depth=self.config.max_depth,
            max_pages=self.config.frontier_budget(),
            default_delay=self.config.delay,
        )
        self._filter = UrlFilter(
            seeds=tuple(self._seeds),
            same_site_only=self.config.same_site_only,
            include_subdomains=self.config.include_subdomains,
            allow_prefixes=self.config.allow_prefixes,
            deny_prefixes=self.config.deny_prefixes,
        )
        self._robots = RobotsCache(
            user_agent=self.config.user_agent,
            default_delay=self.config.delay,
            timeout=self.config.timeout,
        )
        self._fetcher = Fetcher(
            user_agent=self.config.user_agent,
            timeout=self.config.timeout,
            max_retries=self.config.max_retries,
            max_bytes=self.config.max_bytes,
        )
        self._dedup = (
            ContentDeduplicator(max_distance=self.config.near_duplicate_distance)
            if self.config.near_duplicates
            else None
        )
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._since_checkpoint = 0

    # ------------------------------------------------------------------ run

    async def run(self) -> CrawlStats:
        """Crawl until the frontier empties or the page budget is spent."""
        if self.config.checkpoint_path and self.config.checkpoint_path.exists():
            self._load_checkpoint(self.config.checkpoint_path)
        else:
            for seed in self._seeds:
                await self._frontier.add(seed, depth=0)

        async with self._fetcher.build_client() as client:
            self._client = client
            workers = [
                asyncio.create_task(self._worker(), name=f"worker-{i}")
                for i in range(max(1, self.config.concurrency))
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                self._client = None

        self.stats.robots_fetches = self._robots.fetches
        self.stats.finish()
        if self.config.checkpoint_path:
            self._save_checkpoint(self.config.checkpoint_path)
        return self.stats

    async def _worker(self) -> None:
        while not self._stop.is_set():
            request = await self._frontier.get()
            if request is None:
                return
            try:
                await self._process(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the crawl
                self.stats.errors[f"worker:{type(exc).__name__}"] += 1
            finally:
                # Always, on every path: a missed decrement means the frontier
                # believes work is still in flight and no worker ever exits.
                await self._frontier.done()

    # -------------------------------------------------------------- one page

    async def _process(self, request: Request) -> None:
        assert self._client is not None
        if self._stop.is_set():
            return
        url = request.url

        if not await self._robots.allowed(self._client, url):
            self.stats.robots_denied += 1
            return

        delay = await self._robots.delay_for(self._client, url)
        self.stats.politeness_seconds += await self._frontier.wait_turn(request.host, delay)

        # Checked after the wait, not only before it: with a rate limit most
        # workers are asleep on a reserved slot when the budget runs out, and
        # without this they would each still fire one more request.
        if self._stop.is_set():
            return

        result = await self._fetcher.fetch(self._client, url, min_wait=delay)
        self.stats.pages_fetched += 1
        self.stats.bytes_downloaded += result.bytes_read
        self.stats.retries += result.attempts - 1
        if result.status is not None:
            self.stats.status_codes[result.status] += 1
        if result.elapsed_ms:
            self.stats.fetch_ms.append(result.elapsed_ms)

        # The budget is enforced in `finally` because it counts *fetches*, and
        # every path below can return early. An earlier version checked it
        # only after a page was stored, so a site of duplicates -- where
        # nothing is ever stored -- ignored max_pages entirely and crawled
        # until the frontier ran dry.
        try:
            if not result.ok:
                self.stats.errors[result.error or "unknown"] += 1
                return

            page = parse_html(result.text, drop_boilerplate=self.config.drop_boilerplate)
            base = page.base_href or result.final_url or url

            # Links before filtering: a page we will not store may still be
            # the only path to pages we want.
            if page.nofollow:
                self.stats.links_found += len(page.links)
                self.stats.skipped_links["meta-nofollow"] += len(page.links)
            else:
                await self._queue_links(page.links, base, request)

            document = self._accept(page, request, result.status, url)
            if document is None:
                return

            if self.writer is not None:
                self.writer.write(document)
            self.stats.pages_stored += 1
            if self.on_page is not None:
                self.on_page(document, self.stats)

            await self._maybe_checkpoint()
        finally:
            await self._enforce_budget()

    async def _enforce_budget(self) -> None:
        """Stop the crawl once the page budget is spent."""
        if self.config.max_pages is None:
            return
        if self.stats.pages_fetched >= self.config.max_pages and not self._stop.is_set():
            self._stop.set()
            await self._frontier.close()

    def _accept(
        self, page: ParsedPage, request: Request, status: int | None, url: str
    ) -> Document | None:
        """Decide whether a parsed page belongs in the corpus."""
        if page.noindex:
            self.stats.noindex += 1
            return None
        if page.word_count < self.config.min_words:
            self.stats.too_short += 1
            return None

        if self._dedup is not None:
            original = self._dedup.check(page.text, url)
            self.stats.duplicates_exact = self._dedup.exact_hits
            self.stats.duplicates_near = self._dedup.near_hits
            if original is not None:
                return None

        return Document(
            text=page.text,
            url=url,
            title=page.title,
            depth=request.depth,
            status=status,
            content_hash=content_hash(page.text),
            word_count=page.word_count,
            language=page.language,
            referrer=request.referrer,
        )

    async def _queue_links(self, hrefs: list[str], base: str, request: Request) -> None:
        """Normalise, filter and queue the links found on a page."""
        self.stats.links_found += len(hrefs)
        candidates: list[str] = []
        for href in hrefs:
            url = normalise(href, base, self.config.strip_trailing_slash)
            if not url:
                self.stats.skipped_links["unparseable"] += 1
                continue
            if not self._filter.allows(url):
                self.stats.skipped_links[self._filter.reason(url)] += 1
                continue
            candidates.append(url)

        # The frontier rejects for reasons of its own (already seen, too deep,
        # over budget). Folding its counters in here is what keeps
        # links_found == links_queued + skipped exactly true.
        before = self._frontier.skipped
        added = await self._frontier.add_many(candidates, request.depth + 1, request.url)
        after = self._frontier.skipped
        for reason, count in after.items():
            delta = count - before.get(reason, 0)
            if delta:
                self.stats.skipped_links[reason] += delta
        self.stats.links_queued += added

    # ----------------------------------------------------------- checkpoints

    async def _maybe_checkpoint(self) -> None:
        if not self.config.checkpoint_path:
            return
        self._since_checkpoint += 1
        if self._since_checkpoint >= self.config.checkpoint_every:
            self._since_checkpoint = 0
            self._save_checkpoint(self.config.checkpoint_path)

    def _save_checkpoint(self, path: Path) -> None:
        """Write enough state to resume. Bloom-filter crawls cannot resume."""
        if not isinstance(self._seen, UrlSet):
            return
        payload = {
            "seen": self._seen.snapshot(),
            "pending": [
                {"url": r.url, "depth": r.depth, "referrer": r.referrer}
                for r in self._frontier.pending()
            ],
            "stats": self.stats.as_dict(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file and moved into place, so an interrupted
        # write cannot leave a half-written checkpoint behind.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)

    def _load_checkpoint(self, path: Path) -> None:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for url in payload.get("seen", []):
            self._seen.add(url)
        self._frontier.restore(
            [
                Request(url=item["url"], depth=item["depth"], referrer=item.get("referrer"))
                for item in payload.get("pending", [])
            ]
        )


async def crawl(config: CrawlConfig, output: str | Path | None = None) -> CrawlStats:
    """Run a crawl, writing a JSONL corpus if ``output`` is given."""
    if output is None:
        return await Crawler(config).run()
    with JsonlWriter(output) as writer:
        return await Crawler(config, writer=writer).run()
