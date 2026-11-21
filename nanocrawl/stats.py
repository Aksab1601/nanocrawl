"""Counters, because a crawl you cannot see is a crawl you cannot trust.

A crawler fails quietly. It finishes, reports "847 pages", and only much later
does someone notice that 3,000 links were dropped as off-site because of a
``www.`` mismatch, or that a third of the fetches were 429s nobody saw. Every
decision the crawler makes to *not* do something is counted here, by reason.

The rule this module exists to enforce: **a number that goes down somewhere
has to show up somewhere else.** Links found minus links queued equals the sum
of the skip reasons. If it does not, something is being lost silently.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CrawlStats"]


@dataclass
class CrawlStats:
    """Everything that happened during a crawl."""

    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    pages_fetched: int = 0
    pages_stored: int = 0
    links_found: int = 0
    links_queued: int = 0
    bytes_downloaded: int = 0
    robots_denied: int = 0
    robots_fetches: int = 0
    duplicates_exact: int = 0
    duplicates_near: int = 0
    too_short: int = 0
    noindex: int = 0
    retries: int = 0
    politeness_seconds: float = 0.0

    status_codes: Counter[int] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    skipped_links: Counter[str] = field(default_factory=Counter)
    fetch_ms: list[float] = field(default_factory=list, repr=False)

    # --------------------------------------------------------------- derived

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(end - self.started_at, 1e-9)

    @property
    def pages_per_second(self) -> float:
        return self.pages_fetched / self.elapsed

    @property
    def duplicates(self) -> int:
        return self.duplicates_exact + self.duplicates_near

    def percentile(self, p: float) -> float:
        """Fetch latency percentile in milliseconds.

        Reported alongside the mean because crawl latency is dominated by a
        tail: most pages come back in milliseconds and a few take the whole
        timeout, and only the tail tells you whether the timeout is set well.
        """
        if not self.fetch_ms:
            return 0.0
        ordered = sorted(self.fetch_ms)
        index = min(int(p / 100 * len(ordered)), len(ordered) - 1)
        return round(ordered[index], 2)

    def finish(self) -> CrawlStats:
        self.finished_at = time.monotonic()
        return self

    # ---------------------------------------------------------------- output

    def as_dict(self) -> dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed, 2),
            "pages_fetched": self.pages_fetched,
            "pages_stored": self.pages_stored,
            "pages_per_second": round(self.pages_per_second, 2),
            "bytes_downloaded": self.bytes_downloaded,
            "links_found": self.links_found,
            "links_queued": self.links_queued,
            "robots_denied": self.robots_denied,
            "robots_fetches": self.robots_fetches,
            "duplicates_exact": self.duplicates_exact,
            "duplicates_near": self.duplicates_near,
            "too_short": self.too_short,
            "noindex": self.noindex,
            "retries": self.retries,
            "politeness_seconds": round(self.politeness_seconds, 2),
            "fetch_ms_p50": self.percentile(50),
            "fetch_ms_p95": self.percentile(95),
            "status_codes": dict(sorted(self.status_codes.items())),
            "errors": dict(self.errors.most_common()),
            "skipped_links": dict(self.skipped_links.most_common()),
        }

    def accounted_for(self) -> bool:
        """Whether every discovered link was either queued or counted as skipped.

        The invariant this module exists for. A test asserts it, so a new skip
        path that forgets to increment a counter fails the build instead of
        quietly losing pages.
        """
        return self.links_found == self.links_queued + sum(self.skipped_links.values())

    def report(self) -> str:
        """A human-readable summary for the end of a run."""
        lines = [
            f"crawled {self.pages_fetched} pages in {self.elapsed:.1f}s "
            f"({self.pages_per_second:.1f} pages/s), stored {self.pages_stored}",
            f"  downloaded   {self.bytes_downloaded / 1e6:.2f} MB, "
            f"fetch p50 {self.percentile(50):.0f} ms / p95 {self.percentile(95):.0f} ms",
            f"  links        {self.links_found} found, {self.links_queued} queued",
            f"  duplicates   {self.duplicates_exact} exact, {self.duplicates_near} near",
            f"  politeness   {self.politeness_seconds:.1f}s spent waiting between requests",
        ]
        if self.robots_denied:
            lines.append(f"  robots.txt   {self.robots_denied} URLs disallowed")
        if self.status_codes:
            codes = ", ".join(f"{code}:{n}" for code, n in sorted(self.status_codes.items()))
            lines.append(f"  status       {codes}")
        if self.errors:
            errors = ", ".join(f"{name}:{n}" for name, n in self.errors.most_common(6))
            lines.append(f"  errors       {errors}")
        if self.skipped_links:
            skips = ", ".join(f"{name}:{n}" for name, n in self.skipped_links.most_common(6))
            lines.append(f"  skipped      {skips}")
        return "\n".join(lines)
