"""Fetching: timeouts, retries, size limits and content types.

Everything here is about failure. A crawler that only works when the network
behaves is a script, not a crawler.
"""

from __future__ import annotations

import time

import pytest

from nanocrawl.fetch import Fetcher
from nanocrawl.testing import SiteServer


class TestSuccess:
    async def test_fetches_a_page(self, site: SiteServer) -> None:
        fetcher = Fetcher(timeout=5)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/"))
        assert result.ok
        assert result.status == 200
        assert "Index" in result.text
        assert result.bytes_read > 0
        assert result.attempts == 1

    async def test_follows_redirects_and_reports_the_destination(self, site: SiteServer) -> None:
        fetcher = Fetcher(timeout=5)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/redirect/start"))
        assert result.ok
        assert result.final_url.endswith("/redirect/end")
        assert result.redirected

    async def test_sends_the_user_agent(self, site: SiteServer) -> None:
        fetcher = Fetcher(user_agent="nanocrawl-test/9.9")
        async with fetcher.build_client() as client:
            assert client.headers["user-agent"] == "nanocrawl-test/9.9"


class TestFailure:
    async def test_404_is_not_retried(self, site: SiteServer) -> None:
        """A 404 will still be a 404 in two seconds."""
        fetcher = Fetcher(max_retries=3, timeout=5)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/missing"))
        assert not result.ok
        assert result.status == 404
        assert result.attempts == 1
        assert site.hits.get("/missing") == 1

    async def test_500_is_retried(self, site: SiteServer) -> None:
        fetcher = Fetcher(max_retries=2, timeout=5, backoff_base=0.01)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/broken"))
        assert not result.ok
        assert result.attempts == 3
        assert site.hits.get("/broken") == 3

    async def test_retries_recover_from_a_transient_failure(self) -> None:
        """The point of retrying: the second attempt succeeds."""
        with SiteServer(flaky_failures=1) as site:
            fetcher = Fetcher(max_retries=2, timeout=5, backoff_base=0.01)
            async with fetcher.build_client() as client:
                result = await fetcher.fetch(client, site.url("/flaky"))
            assert result.ok
            assert result.attempts == 2

    async def test_gives_up_after_max_retries(self) -> None:
        with SiteServer(flaky_failures=99) as site:
            fetcher = Fetcher(max_retries=2, timeout=5, backoff_base=0.01)
            async with fetcher.build_client() as client:
                result = await fetcher.fetch(client, site.url("/flaky"))
            assert not result.ok
            assert result.attempts == 3
            assert result.error == "http-503"

    async def test_timeout_is_enforced(self, site: SiteServer) -> None:
        fetcher = Fetcher(timeout=0.3, max_retries=0)
        started = time.monotonic()
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/slow"))
        elapsed = time.monotonic() - started
        assert not result.ok
        assert result.error == "timeout"
        assert elapsed < 2.0, "the 2s page should have been abandoned"

    async def test_unreachable_host_is_reported_not_raised(self) -> None:
        fetcher = Fetcher(timeout=0.5, max_retries=0, backoff_base=0.01)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, "http://127.0.0.1:1/nope")
        assert not result.ok
        assert result.error is not None


class TestLimits:
    async def test_oversized_bodies_are_abandoned(self, site: SiteServer) -> None:
        fetcher = Fetcher(max_bytes=50_000, timeout=10)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/huge"))
        # Declared Content-Length exceeds the cap, so it is refused before
        # the body is read at all.
        assert not result.ok
        assert result.error == "too-large"

    async def test_non_html_is_skipped(self, site: SiteServer) -> None:
        fetcher = Fetcher(timeout=5)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/report.pdf"))
        assert not result.ok
        assert result.error is not None
        assert result.error.startswith("content-type:")

    async def test_elapsed_time_is_recorded(self, site: SiteServer) -> None:
        fetcher = Fetcher(timeout=5)
        async with fetcher.build_client() as client:
            result = await fetcher.fetch(client, site.url("/"))
        assert result.elapsed_ms > 0


class TestBackoff:
    def test_backoff_grows_and_stays_capped(self) -> None:
        fetcher = Fetcher(backoff_base=0.5, backoff_cap=4.0)
        for attempt in range(8):
            assert 0 <= fetcher._sleep_for(attempt, None) <= 4.0

    def test_retry_after_header_wins(self) -> None:
        fetcher = Fetcher(backoff_base=0.5, backoff_cap=4.0)
        assert fetcher._sleep_for(0, "2") == pytest.approx(2.0)

    def test_unparseable_retry_after_falls_back_to_backoff(self) -> None:
        fetcher = Fetcher(backoff_base=0.5, backoff_cap=4.0)
        assert 0 <= fetcher._sleep_for(1, "Wed, 21 Oct 2026 07:28:00 GMT") <= 4.0

    def test_jitter_makes_waits_differ(self) -> None:
        """Without jitter, workers that failed together retry together."""
        fetcher = Fetcher(backoff_base=1.0, backoff_cap=8.0)
        waits = {round(fetcher._sleep_for(3, None), 6) for _ in range(20)}
        assert len(waits) > 1
