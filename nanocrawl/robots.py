"""robots.txt: asking permission before fetching.

Every host gets its ``/robots.txt`` fetched once, parsed, and cached for the
rest of the crawl. The file says which paths a given user-agent may fetch and,
sometimes, how slowly.

**There is no option to turn this off.** That is a deliberate design decision,
not an oversight. A crawler that ignores robots.txt is the difference between
a tool and a nuisance: it gets IP-banned, it costs the site owner money, and
in several jurisdictions it is the fact that turns unauthorised access into a
legal question. The flag would be used exactly once, in a hurry, and then
forgotten.

Behaviour follows RFC 9309:

* **2xx** -- parse and apply the rules
* **404 / 410** -- no file means no restrictions; crawl freely
* **401 / 403** -- access to the rules themselves is forbidden, so treat the
  whole site as disallowed
* **5xx or network failure** -- the server cannot tell us the rules, so assume
  the strictest reading and stay away. Failing *closed* is the only safe
  default: failing open means a site having a bad day gets crawled against
  rules nobody could read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

__all__ = ["RobotsCache", "RobotsRules"]


@dataclass
class RobotsRules:
    """Parsed rules for one host."""

    parser: RobotFileParser | None
    allow_all: bool = False
    deny_all: bool = False
    status: int | None = None
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()

    def allows(self, url: str, user_agent: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all or self.parser is None:
            return True
        return bool(self.parser.can_fetch(user_agent, url))


@dataclass
class RobotsCache:
    """Fetches, parses and caches robots.txt, once per host.

    Concurrency note: a crawl starts by firing every worker at the same host
    at once, so without care each of them would fetch robots.txt
    simultaneously -- a thundering herd against the one file whose point is to
    reduce load. A per-host lock means the first worker fetches and the rest
    wait for that result.
    """

    user_agent: str = "nanocrawl"
    default_delay: float = 0.5
    timeout: float = 10.0
    _rules: dict[str, RobotsRules] = field(default_factory=dict, repr=False)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    fetches: int = 0

    def _origin(self, url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def rules_for(self, client: httpx.AsyncClient, url: str) -> RobotsRules:
        """Rules for the host of ``url``, fetching them once per host."""
        origin = self._origin(url)
        cached = self._rules.get(origin)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            # Re-check: another worker may have populated it while we waited.
            cached = self._rules.get(origin)
            if cached is not None:
                return cached
            rules = await self._fetch(client, origin)
            self._rules[origin] = rules
            return rules

    async def _fetch(self, client: httpx.AsyncClient, origin: str) -> RobotsRules:
        self.fetches += 1
        try:
            response = await client.get(origin, timeout=self.timeout)
        except (httpx.HTTPError, OSError):
            # Could not reach the rules: fail closed.
            return RobotsRules(parser=None, deny_all=True, status=None)

        status = response.status_code
        if status in (401, 403):
            return RobotsRules(parser=None, deny_all=True, status=status)
        if status >= 500:
            return RobotsRules(parser=None, deny_all=True, status=status)
        if status >= 400:
            return RobotsRules(parser=None, allow_all=True, status=status)

        parser = RobotFileParser()
        text = response.text
        parser.parse(text.splitlines())

        delay = parser.crawl_delay(self.user_agent)
        rate = parser.request_rate(self.user_agent)
        crawl_delay: float | None = float(delay) if delay is not None else None
        if crawl_delay is None and rate is not None and rate.requests > 0:
            crawl_delay = float(rate.seconds) / float(rate.requests)

        sitemaps = tuple(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.lower().startswith("sitemap:")
        )
        return RobotsRules(parser=parser, status=status, crawl_delay=crawl_delay, sitemaps=sitemaps)

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        """Whether this crawler may fetch ``url``."""
        rules = await self.rules_for(client, url)
        return rules.allows(url, self.user_agent)

    async def delay_for(self, client: httpx.AsyncClient, url: str) -> float:
        """Seconds to wait between requests to this host.

        The site's own ``Crawl-delay`` wins when it is *slower* than our
        default. A site asking for 10 seconds gets 10 seconds; a site asking
        for 0.1 still gets our default, because being asked to go fast is not
        a reason to hammer someone.
        """
        rules = await self.rules_for(client, url)
        if rules.crawl_delay is None:
            return self.default_delay
        return max(self.default_delay, rules.crawl_delay)

    def known_hosts(self) -> int:
        return len(self._rules)
