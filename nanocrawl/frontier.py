"""The frontier: what to crawl next, and when we are allowed to.

Two jobs, and they are usually confused with each other:

**Order.** The frontier is a queue of discovered URLs. Popping from the front
gives breadth-first order -- the seed page, then everything it links to, then
everything *those* link to. Breadth-first is the right default for a crawler
because the pages nearest the seed are usually the ones that matter, so a
crawl cut short still got the important pages. A stack (depth-first) would
wander into one deep branch and never come back.

**Politeness.** A crawler can issue requests far faster than a site can serve
them, and a fast crawler is indistinguishable from a denial-of-service attack.
Every host therefore has a minimum interval between requests, and this module
is what enforces it.

The two are kept apart on purpose: *which* URL is next is a queueing question,
*when* it may be fetched is a scheduling one.

Fairness across hosts
---------------------
URLs are kept in one queue per host and popped round-robin, so a site with
10,000 pages cannot starve a site with 10. With a single-host crawl -- the
default -- this costs nothing and does nothing; with several seeds it is the
difference between a crawl that finishes and one that spends an hour on the
first domain it met.

Slot reservation
----------------
Rather than making workers wait their turn on a lock, each worker *reserves*
the next free moment for a host and sleeps until it:

    earliest = max(now, next_allowed[host])
    next_allowed[host] = earliest + delay
    sleep(earliest - now)

The reservation is instant and under a lock; the sleeping is not. So ten
workers hitting one host get ten distinct slots spaced ``delay`` apart,
throughput settles at exactly ``1/delay`` requests per second per host no
matter how many workers there are, and no worker ever holds a lock while
sleeping.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

__all__ = ["Frontier", "Request", "SeenUrls"]


class SeenUrls(Protocol):
    """Anything that can answer "have I queued this URL before?"."""

    def add(self, url: str) -> bool:
        """Record ``url``; return True if it had not been seen."""

    def __len__(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Request:
    """One URL waiting to be fetched."""

    url: str
    depth: int = 0
    referrer: str | None = None

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass
class Frontier:
    """A breadth-first, per-host-fair, politeness-aware work queue.

    Parameters
    ----------
    seen:
        The set of URLs already queued. Injected rather than built here so a
        crawl can swap a plain set for a Bloom filter, or restore one from a
        checkpoint.
    max_depth:
        How many links from a seed to follow. Depth 0 is the seed itself.
    max_pages:
        Stop accepting new URLs after this many have been queued. This bounds
        memory as well as work: an unbounded frontier on a site with a
        calendar widget grows until the process dies.
    default_delay:
        Seconds between requests to one host, unless its robots.txt asks for
        more.
    """

    seen: SeenUrls
    max_depth: int = 3
    max_pages: int | None = None
    default_delay: float = 0.5

    _hosts: dict[str, deque[Request]] = field(default_factory=dict, repr=False)
    _order: deque[str] = field(default_factory=deque, repr=False)
    _next_allowed: dict[str, float] = field(default_factory=dict, repr=False)
    _queued: int = 0
    _in_flight: int = 0
    _accepted: int = 0
    _finished: bool = False
    _skipped: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._cv = asyncio.Condition()
        self._slot_lock = asyncio.Lock()

    # ------------------------------------------------------------ properties

    @property
    def queued(self) -> int:
        return self._queued

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def accepted(self) -> int:
        """How many URLs have ever been queued."""
        return self._accepted

    @property
    def skipped(self) -> dict[str, int]:
        """Counts of URLs not queued, by reason."""
        return dict(self._skipped)

    def _skip(self, reason: str) -> None:
        self._skipped[reason] = self._skipped.get(reason, 0) + 1

    # --------------------------------------------------------------- adding

    async def add(self, url: str, depth: int = 0, referrer: str | None = None) -> bool:
        """Queue ``url`` unless it is a duplicate, too deep, or over budget."""
        async with self._cv:
            accepted = self._add_locked(url, depth, referrer)
            if accepted:
                self._cv.notify()
            return accepted

    async def add_many(self, urls: list[str], depth: int, referrer: str | None = None) -> int:
        """Queue a page's worth of links in one lock acquisition."""
        added = 0
        async with self._cv:
            for url in urls:
                if self._add_locked(url, depth, referrer):
                    added += 1
            if added:
                self._cv.notify_all()
        return added

    def _add_locked(self, url: str, depth: int, referrer: str | None) -> bool:
        if depth > self.max_depth:
            self._skip("too-deep")
            return False
        if self.max_pages is not None and self._accepted >= self.max_pages:
            self._skip("over-budget")
            return False
        # The seen-set is checked last: the cheap rejections above should not
        # pollute it, or a URL rejected for depth could never be queued again
        # if it is later found at a shallower depth.
        if not self.seen.add(url):
            self._skip("duplicate-url")
            return False

        request = Request(url=url, depth=depth, referrer=referrer)
        queue = self._hosts.get(request.host)
        if queue is None:
            queue = deque()
            self._hosts[request.host] = queue
            self._order.append(request.host)
        queue.append(request)
        self._queued += 1
        self._accepted += 1
        return True

    # -------------------------------------------------------------- popping

    async def get(self) -> Request | None:
        """Next URL to fetch, or ``None`` when the crawl is finished.

        "Finished" means nothing is queued *and* nothing is in flight -- a
        worker still fetching may yet discover more links, so an empty queue
        alone proves nothing. Getting this wrong is the classic crawler bug:
        workers exit early and the crawl silently stops half-done.
        """
        async with self._cv:
            while True:
                # Checked before the queue: an early stop leaves URLs queued
                # on purpose, so that a checkpoint can resume from them.
                if self._finished:
                    self._cv.notify_all()
                    return None
                if self._queued:
                    self._in_flight += 1
                    return self._pop_locked()
                if self._in_flight == 0:
                    self._finished = True
                    self._cv.notify_all()
                    return None
                await self._cv.wait()

    def _pop_locked(self) -> Request:
        host = self._order.popleft()
        queue = self._hosts[host]
        request = queue.popleft()
        if queue:
            self._order.append(host)  # back of the rotation
        else:
            del self._hosts[host]
        self._queued -= 1
        return request

    async def done(self) -> None:
        """Report that an in-flight request has finished, however it ended."""
        async with self._cv:
            self._in_flight -= 1
            if self._in_flight <= 0 and not self._queued:
                self._finished = True
            self._cv.notify_all()

    async def close(self) -> None:
        """Stop the crawl early; wake every waiting worker.

        Whatever is still queued is deliberately kept rather than discarded:
        it is exactly the work a checkpoint needs in order to resume.
        """
        async with self._cv:
            self._finished = True
            self._cv.notify_all()

    # ---------------------------------------------------------- checkpoints

    def pending(self) -> list[Request]:
        """Everything still queued, for saving to a checkpoint.

        In-flight requests are deliberately not included: they are re-queued
        from the checkpoint's seen-set exclusion on resume only if they never
        completed, which is the safe direction to be wrong in -- a page
        fetched twice costs one request, a page never fetched is lost.
        """
        return [request for host in self._order for request in self._hosts[host]]

    def restore(self, requests: list[Request]) -> None:
        """Re-queue saved work, bypassing the seen-set.

        Those URLs are already in the restored seen-set, so the normal path
        would reject every one of them as a duplicate.
        """
        for request in requests:
            queue = self._hosts.get(request.host)
            if queue is None:
                queue = deque()
                self._hosts[request.host] = queue
                self._order.append(request.host)
            queue.append(request)
            self._queued += 1
            self._accepted += 1

    # ----------------------------------------------------------- politeness

    async def reserve(self, host: str, delay: float | None = None) -> float:
        """Claim the next free moment for ``host``; return how long to wait.

        The caller sleeps for the returned number of seconds. Reserving and
        waiting are separate so that the lock is held for microseconds rather
        than for the whole delay.
        """
        wait_for = self.default_delay if delay is None else max(delay, 0.0)
        async with self._slot_lock:
            now = time.monotonic()
            earliest = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = earliest + wait_for
            return earliest - now

    async def wait_turn(self, host: str, delay: float | None = None) -> float:
        """Reserve a slot and sleep until it. Returns the seconds slept."""
        pause = await self.reserve(host, delay)
        if pause > 0:
            await asyncio.sleep(pause)
        return max(pause, 0.0)
