"""robots.txt handling, including what happens when it cannot be read.

The failure cases matter more than the happy path: how a crawler behaves when
the rules are unavailable is the difference between cautious and reckless.
"""

from __future__ import annotations

import httpx
import pytest

from nanocrawl.robots import RobotsCache
from nanocrawl.testing import SitePage, SiteServer

PLAIN = "text/plain; charset=utf-8"


def site_with_robots(body: str, status: int = 200) -> SiteServer:
    return SiteServer(
        pages={
            "/robots.txt": SitePage(body, status=status, content_type=PLAIN),
            "/": SitePage("<p>home</p>"),
            "/private/secret": SitePage("<p>secret</p>"),
            "/public/open": SitePage("<p>open</p>"),
        }
    )


class TestRules:
    async def test_disallowed_paths_are_refused(self) -> None:
        with site_with_robots("User-agent: *\nDisallow: /private/\n") as site:
            cache = RobotsCache(user_agent="nanocrawl")
            async with httpx.AsyncClient() as client:
                assert await cache.allowed(client, site.url("/public/open"))
                assert not await cache.allowed(client, site.url("/private/secret"))

    async def test_rules_for_a_named_agent_win(self) -> None:
        body = (
            "User-agent: *\nDisallow: /\n\nUser-agent: nanocrawl\nDisallow: /private/\nAllow: /\n"
        )
        with site_with_robots(body) as site:
            cache = RobotsCache(user_agent="nanocrawl")
            async with httpx.AsyncClient() as client:
                assert await cache.allowed(client, site.url("/public/open"))
                assert not await cache.allowed(client, site.url("/private/secret"))

    async def test_crawl_delay_is_read(self) -> None:
        with site_with_robots("User-agent: *\nCrawl-delay: 3\n") as site:
            cache = RobotsCache(user_agent="nanocrawl", default_delay=0.5)
            async with httpx.AsyncClient() as client:
                assert await cache.delay_for(client, site.url("/")) == 3.0

    async def test_a_faster_crawl_delay_does_not_speed_us_up(self) -> None:
        """Being invited to go faster is not a reason to."""
        with site_with_robots("User-agent: *\nCrawl-delay: 0\n") as site:
            cache = RobotsCache(user_agent="nanocrawl", default_delay=0.5)
            async with httpx.AsyncClient() as client:
                assert await cache.delay_for(client, site.url("/")) == 0.5

    async def test_sitemaps_are_collected(self) -> None:
        body = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
        with site_with_robots(body) as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                rules = await cache.rules_for(client, site.url("/"))
        assert rules.sitemaps == ("https://example.com/sitemap.xml",)


class TestMissingOrBrokenRules:
    async def test_404_means_no_restrictions(self) -> None:
        with site_with_robots("not found", status=404) as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                assert await cache.allowed(client, site.url("/anything"))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_forbidden_rules_mean_the_whole_site_is_off_limits(self, status: int) -> None:
        with site_with_robots("denied", status=status) as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                assert not await cache.allowed(client, site.url("/anything"))

    async def test_server_error_fails_closed(self) -> None:
        """A site having a bad day is not permission to crawl it."""
        with site_with_robots("boom", status=500) as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                assert not await cache.allowed(client, site.url("/anything"))

    async def test_unreachable_host_fails_closed(self) -> None:
        cache = RobotsCache(timeout=0.5)
        async with httpx.AsyncClient() as client:
            # Port 1 on localhost: nothing is listening, so the connection is
            # refused immediately.
            assert not await cache.allowed(client, "http://127.0.0.1:1/page")


class TestCaching:
    async def test_robots_is_fetched_once_per_host(self) -> None:
        with site_with_robots("User-agent: *\nAllow: /\n") as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                for path in ("/a", "/b", "/c", "/d"):
                    await cache.allowed(client, site.url(path))
            assert site.hits.get("/robots.txt", 0) == 1
            assert cache.fetches == 1

    async def test_concurrent_checks_do_not_stampede(self) -> None:
        """Every worker starts at once; only one of them should fetch."""
        import asyncio

        with site_with_robots("User-agent: *\nAllow: /\n") as site:
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                await asyncio.gather(
                    *(cache.allowed(client, site.url(f"/p{i}")) for i in range(20))
                )
            assert site.hits.get("/robots.txt", 0) == 1

    async def test_each_host_gets_its_own_rules(self) -> None:
        with (
            site_with_robots("User-agent: *\nDisallow: /private/\n") as first,
            site_with_robots("User-agent: *\nAllow: /\n") as second,
        ):
            cache = RobotsCache()
            async with httpx.AsyncClient() as client:
                assert not await cache.allowed(client, first.url("/private/secret"))
                assert await cache.allowed(client, second.url("/private/secret"))
            assert cache.known_hosts() == 2
