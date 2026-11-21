"""Fetching one page: timeouts, retries, size limits and honest failure.

The network is the part of a crawler that goes wrong. Pages hang, servers rate
limit, DNS fails, a "web page" turns out to be a 2 GB video, and a redirect
chain loops back on itself. Everything here exists because of one of those.

The rules:

* **Every request has a timeout.** Without one, a single unresponsive server
  holds a worker forever and the crawl quietly loses capacity until it stops.
* **Retries are for transient failures only.** A 404 will still be a 404 in
  two seconds; retrying it wastes the site's capacity and ours. Network
  errors, 429 and 5xx are worth another try.
* **Backoff is exponential with full jitter.** Doubling the wait gives a
  struggling server room to recover; the randomness stops a batch of workers
  that all failed at the same moment from retrying in lockstep and hitting it
  with another synchronised wave.
* **``Retry-After`` wins.** When a server says how long to wait, that is not a
  suggestion to average with our own backoff.
* **Responses are capped and streamed.** The body is read in chunks and
  abandoned once it exceeds the limit, so a huge file costs a few kilobytes of
  transfer rather than memory exhaustion.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

__all__ = ["FetchResult", "Fetcher"]

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class FetchResult:
    """What happened when we asked for a URL."""

    url: str
    final_url: str = ""
    status: int | None = None
    ok: bool = False
    text: str = ""
    content_type: str = ""
    elapsed_ms: float = 0.0
    attempts: int = 0
    bytes_read: int = 0
    truncated: bool = False
    error: str | None = None

    @property
    def redirected(self) -> bool:
        return bool(self.final_url) and self.final_url != self.url


@dataclass
class Fetcher:
    """Fetches pages with retries, bounded size and a declared identity.

    Parameters
    ----------
    user_agent:
        Sent on every request. A crawler that does not identify itself cannot
        be contacted when it misbehaves, and is the first thing a site blocks.
        Include a URL or an email in production.
    max_bytes:
        Hard cap on a response body. 2 MB is far more than any article.
    max_retries:
        Additional attempts after the first, for transient failures only.
    """

    user_agent: str = "nanocrawl/0.1 (+https://github.com/)"
    timeout: float = 15.0
    max_retries: int = 2
    max_bytes: int = 2_000_000
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    accept_types: frozenset[str] = HTML_TYPES
    max_redirects: int = 5
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    # Set by the last attempt when a server sent Retry-After.
    _last_retry_after: str | None = field(default=None, repr=False)

    def build_client(self, **kwargs: object) -> httpx.AsyncClient:
        """An ``httpx`` client configured the way the crawler needs it."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        }
        defaults: dict[str, object] = {
            "headers": headers,
            "follow_redirects": True,
            "max_redirects": self.max_redirects,
            "timeout": httpx.Timeout(self.timeout, connect=min(self.timeout, 10.0)),
            # Connection reuse matters: a fresh TLS handshake per page can
            # cost more than the page itself.
            "limits": httpx.Limits(max_connections=100, max_keepalive_connections=20),
        }
        defaults.update(kwargs)
        return httpx.AsyncClient(**defaults)  # type: ignore[arg-type]

    def _sleep_for(self, attempt: int, retry_after: str | None, min_wait: float = 0.0) -> float:
        """How long to wait before the next attempt.

        ``min_wait`` is the host's politeness delay. Without it a retry would
        be the one request that ignores the rate limit -- and retries happen
        exactly when a server is already struggling, which is the worst
        possible moment to stop being polite.
        """
        if retry_after:
            try:
                return max(min(float(retry_after), self.backoff_cap * 4), min_wait)
            except ValueError:
                pass  # a date form, which we do not parse; fall through
        ceiling = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return max(self._rng.uniform(0, ceiling), min_wait)

    async def fetch(
        self, client: httpx.AsyncClient, url: str, min_wait: float = 0.0
    ) -> FetchResult:
        """Fetch ``url``, retrying transient failures.

        ``min_wait`` floors the gap between attempts at the host's politeness
        delay, so retries cannot outpace the rate limit.
        """
        result = FetchResult(url=url)
        started = time.perf_counter()

        for attempt in range(self.max_retries + 1):
            result.attempts = attempt + 1
            try:
                outcome = await self._attempt(client, url, result)
            except httpx.TimeoutException:
                result.error = "timeout"
                outcome = "retry"
            except httpx.TooManyRedirects:
                result.error = "too-many-redirects"
                outcome = "stop"
            except (httpx.HTTPError, OSError) as exc:
                result.error = f"{type(exc).__name__.lower()}"
                outcome = "retry"

            if outcome in ("done", "stop"):
                break
            if attempt < self.max_retries:
                header = None
                if result.status in (429, 503):
                    header = self._last_retry_after
                await asyncio.sleep(self._sleep_for(attempt, header, min_wait))

        result.elapsed_ms = round((time.perf_counter() - started) * 1e3, 2)
        return result

    async def _attempt(self, client: httpx.AsyncClient, url: str, result: FetchResult) -> str:
        """One request. Returns ``done``, ``retry`` or ``stop``."""
        self._last_retry_after = None
        async with client.stream("GET", url) as response:
            result.status = response.status_code
            result.final_url = str(response.url)
            result.content_type = (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
            )

            if response.status_code in RETRYABLE_STATUS:
                self._last_retry_after = response.headers.get("retry-after")
                result.error = f"http-{response.status_code}"
                return "retry"
            if response.status_code >= 400:
                result.error = f"http-{response.status_code}"
                return "stop"

            if result.content_type and result.content_type not in self.accept_types:
                result.error = f"content-type:{result.content_type}"
                return "stop"

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                result.error = "too-large"
                result.bytes_read = int(declared)
                return "stop"

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.max_bytes:
                    result.truncated = True
                    break
                chunks.append(chunk)

            body = b"".join(chunks)
            result.bytes_read = total
            encoding = response.charset_encoding or "utf-8"
            try:
                result.text = body.decode(encoding, errors="replace")
            except LookupError:  # a charset Python does not know
                result.text = body.decode("utf-8", errors="replace")
            result.ok = True
            result.error = None
            return "done"
