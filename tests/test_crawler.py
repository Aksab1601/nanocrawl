"""End-to-end crawls against the fake site.

These are the tests that matter. The unit tests prove each piece works; these
prove the pieces together do the right thing to somebody's server -- which is
the only claim the project actually makes.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from nanocrawl.crawler import CrawlConfig, Crawler, crawl
from nanocrawl.store import JsonlWriter, read_corpus
from nanocrawl.testing import SiteServer


def config(site: SiteServer, **overrides: object) -> CrawlConfig:
    options: dict = {
        "seeds": [site.url("/")],
        "max_pages": 100,
        "max_depth": 3,
        "concurrency": 4,
        "delay": 0.0,
        "timeout": 5.0,
        "min_words": 20,
    }
    options.update(overrides)
    return CrawlConfig(**options)  # type: ignore[arg-type]


class TestItWorks:
    async def test_crawls_the_site_and_terminates(self, site: SiteServer) -> None:
        stats = await asyncio.wait_for(Crawler(config(site)).run(), timeout=60)
        assert stats.pages_fetched > 10
        assert stats.pages_stored > 5

    async def test_writes_a_usable_corpus(self, site: SiteServer, tmp_path: Path) -> None:
        output = tmp_path / "corpus.jsonl"
        stats = await crawl(config(site), output=output)

        records = list(read_corpus(output))
        assert len(records) == stats.pages_stored
        for record in records:
            assert record["text"].strip()
            assert record["url"].startswith(site.origin)
            assert record["word_count"] >= 20
            assert record["content_hash"]
            assert record["fetched_at"]

    async def test_every_url_is_fetched_at_most_once(self, site: SiteServer) -> None:
        """The cycle test: the site is full of links back to pages already seen."""
        await crawl(config(site))
        repeated = {
            path: count
            for path, count in site.hits.items()
            if count > 1 and not path.startswith("/robots.txt")
        }
        # /broken is retried on purpose; nothing else should repeat.
        assert set(repeated) <= {"/broken"}, repeated

    async def test_link_accounting_balances(self, site: SiteServer) -> None:
        stats = await crawl(config(site))
        assert stats.accounted_for(), (
            stats.links_found,
            stats.links_queued,
            dict(stats.skipped_links),
        )


class TestPoliteness:
    async def test_robots_disallowed_paths_are_never_requested(self, site: SiteServer) -> None:
        """The single most important assertion in the suite."""
        stats = await crawl(config(site))
        assert not [path for path in site.hits if path.startswith("/private/")]
        assert stats.robots_denied >= 1

    async def test_robots_is_fetched_once(self, site: SiteServer) -> None:
        await crawl(config(site))
        assert site.hits.get("/robots.txt") == 1

    async def test_the_delay_is_honoured_on_the_wire(self, site: SiteServer) -> None:
        """Measured at the server, not trusted from the crawler's own counters."""
        delay = 0.15
        await crawl(config(site, delay=delay, max_pages=8, concurrency=4, max_retries=0))

        moments = [moment for moment, path in site.timeline if not path.startswith("/robots.txt")]
        gaps = [b - a for a, b in zip(moments, moments[1:], strict=False)]
        assert len(gaps) >= 5
        # Allow a little scheduling slack, but no gap may be near zero.
        assert min(gaps) > delay * 0.7, sorted(gaps)[:3]

    async def test_concurrency_does_not_defeat_the_rate_limit(self, site: SiteServer) -> None:
        """Sixteen workers on one host still produce one request per delay."""
        delay = 0.1
        started = time.monotonic()
        stats = await crawl(config(site, delay=delay, concurrency=16, max_pages=10, max_retries=0))
        elapsed = time.monotonic() - started
        # Ten pages at one per 0.1s cannot finish faster than ~0.9s however
        # many workers are waiting.
        assert elapsed > (stats.pages_fetched - 1) * delay * 0.7


class TestLimits:
    async def test_max_pages_is_respected(self, site: SiteServer) -> None:
        stats = await crawl(config(site, max_pages=5, concurrency=2))
        # Workers already in flight may finish, so allow a small overshoot.
        assert 5 <= stats.pages_fetched <= 5 + 2

    async def test_max_pages_holds_even_when_nothing_is_stored(self) -> None:
        """A regression: the budget counts fetches, not stores.

        On a site where every page is a duplicate, nothing is ever stored. An
        earlier version checked the budget only after a successful store, so
        max_pages was silently ignored and the crawl ran until the frontier
        emptied -- the exact failure a page budget exists to prevent.
        """
        from nanocrawl.testing import SitePage

        body = "the same paragraph of text on every single page " * 25
        pages = {
            "/robots.txt": SitePage("User-agent: *\nAllow: /\n", content_type="text/plain"),
            "/": SitePage(
                f"<main><p>{body}</p>"
                + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(60))
                + "</main>"
            ),
        }
        for i in range(60):
            pages[f"/p{i}"] = SitePage(f"<main><p>{body}</p></main>")

        with SiteServer(pages=pages) as server:
            stats = await crawl(
                CrawlConfig(
                    seeds=[server.url("/")],
                    max_pages=8,
                    max_depth=2,
                    concurrency=2,
                    delay=0.0,
                    timeout=5.0,
                )
            )
        assert stats.pages_stored <= 1  # everything after the first is a duplicate
        assert stats.pages_fetched <= 8 + 2, stats.pages_fetched

    async def test_max_depth_bounds_the_parameter_trap(self, site: SiteServer) -> None:
        """/calendar?day=N is an infinite URL space; depth is what survives it."""
        stats = await crawl(config(site, max_depth=2, max_pages=500))
        calendar = [path for path in site.hits if path.startswith("/calendar")]
        assert len(calendar) < 10, calendar
        assert stats.skipped_links.get("too-deep", 0) > 0

    async def test_off_site_links_are_not_followed(self, site: SiteServer) -> None:
        stats = await crawl(config(site))
        assert stats.skipped_links.get("off-site", 0) >= 1

    async def test_binary_links_are_not_fetched(self, site: SiteServer) -> None:
        await crawl(config(site))
        assert "/report.pdf" not in site.hits


class TestContentRules:
    async def test_noindex_pages_are_not_stored(self, site: SiteServer, tmp_path: Path) -> None:
        output = tmp_path / "corpus.jsonl"
        stats = await crawl(config(site), output=output)
        urls = {record["url"] for record in read_corpus(output)}
        assert not any(url.endswith("/noindex") for url in urls)
        assert stats.noindex >= 1

    async def test_links_from_noindex_pages_are_still_followed(self, site: SiteServer) -> None:
        """noindex means "do not index me", not "stop crawling here"."""
        await crawl(config(site))
        assert "/only-reachable-from-noindex" in site.hits

    async def test_nofollow_links_are_not_followed(self, site: SiteServer) -> None:
        await crawl(config(site))
        assert "/never-followed" not in site.hits

    async def test_duplicates_are_detected(self, site: SiteServer, tmp_path: Path) -> None:
        output = tmp_path / "corpus.jsonl"
        stats = await crawl(config(site), output=output)
        assert stats.duplicates_exact >= 1
        assert stats.duplicates_near >= 1

        urls = {record["url"] for record in read_corpus(output)}
        assert not any(url.endswith("/duplicate-of-0") for url in urls)

    async def test_duplicate_detection_can_be_disabled(self, site: SiteServer) -> None:
        stats = await crawl(config(site, near_duplicates=False))
        assert stats.duplicates_exact == 0
        assert stats.duplicates_near == 0

    async def test_short_pages_are_dropped(self, site: SiteServer) -> None:
        stats = await crawl(config(site, min_words=10_000))
        assert stats.pages_stored == 0
        assert stats.too_short > 0


class TestErrors:
    async def test_error_pages_do_not_stop_the_crawl(self, site: SiteServer) -> None:
        stats = await crawl(config(site))
        assert stats.status_codes.get(404, 0) >= 1
        assert stats.status_codes.get(500, 0) >= 1
        assert stats.pages_stored > 5  # carried on regardless

    async def test_a_slow_page_does_not_block_the_crawl(self, site: SiteServer) -> None:
        stats = await crawl(config(site, timeout=0.3, max_retries=0))
        assert "timeout" in stats.errors
        assert stats.pages_stored > 5

    async def test_unreachable_seed_is_reported_not_raised(self) -> None:
        stats = await crawl(
            CrawlConfig(seeds=["http://127.0.0.1:1/"], max_pages=5, delay=0.0, timeout=0.5)
        )
        assert stats.pages_fetched == 0

    async def test_rejects_an_empty_seed_list(self) -> None:
        with pytest.raises(ValueError, match="no usable seed"):
            Crawler(CrawlConfig(seeds=[]))

    async def test_rejects_unusable_seeds(self) -> None:
        with pytest.raises(ValueError, match="no usable seed"):
            Crawler(CrawlConfig(seeds=["mailto:a@b.com"]))


class TestConcurrency:
    @pytest.mark.parametrize("workers", [1, 4, 16])
    async def test_results_do_not_depend_on_worker_count(
        self, site: SiteServer, workers: int
    ) -> None:
        stats = await crawl(config(site, concurrency=workers, max_pages=500))
        assert stats.pages_fetched > 10
        assert stats.accounted_for()

    async def test_more_workers_is_faster_when_the_site_allows_it(self) -> None:
        """With no rate limit, concurrency should actually buy something.

        Every page is given the same deliberate delay, so the two runs fetch
        identical work and the only variable is how many workers do it. The
        earlier version of this test compared different page sets and was
        measuring which pages happened to be reached, not concurrency.
        """
        from nanocrawl.testing import SitePage

        pages = {
            "/robots.txt": SitePage("User-agent: *\nAllow: /\n", content_type="text/plain"),
            "/": SitePage(
                "<main><p>"
                + "index words here " * 30
                + "</p>"
                + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(12))
                + "</main>",
                delay=0.1,
            ),
        }
        for i in range(12):
            pages[f"/p{i}"] = SitePage(
                f"<main><p>page {i} " + "body words here " * 30 + "</p></main>", delay=0.1
            )

        timings = {}
        for workers in (1, 8):
            with SiteServer(pages=pages) as server:
                started = time.monotonic()
                stats = await crawl(config(server, concurrency=workers, max_pages=13, delay=0.0))
                timings[workers] = time.monotonic() - started
                assert stats.pages_fetched == 13

        # 13 pages at 0.1s each: ~1.3s serially, a fraction of that in parallel.
        assert timings[1] > 1.0
        assert timings[8] < timings[1] / 2, timings


class TestCheckpointing:
    async def test_a_crawl_can_resume_where_it_stopped(
        self, site: SiteServer, tmp_path: Path
    ) -> None:
        state = tmp_path / "state.json"

        first = await crawl(config(site, max_pages=6, checkpoint_path=state, concurrency=2))
        assert state.exists()
        assert first.pages_fetched >= 6

        payload = json.loads(state.read_text())
        assert payload["seen"]
        assert payload["pending"]

        before = sum(site.hits.values())
        second = await crawl(config(site, max_pages=6, checkpoint_path=state, concurrency=2))
        after = sum(site.hits.values())

        # The resumed crawl fetched new pages, not the ones already done.
        assert second.pages_fetched > 0
        assert after > before
        repeated = [
            path
            for path, count in site.hits.items()
            if count > 1 and path not in ("/robots.txt", "/broken")
        ]
        assert not repeated, repeated


class TestOutput:
    async def test_writer_flushes_as_it_goes(self, site: SiteServer, tmp_path: Path) -> None:
        """A crawl killed halfway must leave a readable corpus behind."""
        output = tmp_path / "corpus.jsonl"
        with JsonlWriter(output) as writer:
            crawler = Crawler(config(site, max_pages=6), writer=writer)
            await crawler.run()
            # Still inside the context: the file must already be complete.
            assert output.read_text().strip().count("\n") + 1 == writer.written

    async def test_stats_serialise_to_json(self, site: SiteServer) -> None:
        stats = await crawl(config(site))
        payload = json.dumps(stats.as_dict())
        restored = json.loads(payload)
        assert restored["pages_fetched"] == stats.pages_fetched
        assert "status_codes" in restored
