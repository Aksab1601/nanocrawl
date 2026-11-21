"""nanocrawl -- a polite, concurrent web crawler that produces a text corpus.

Quick start::

    import asyncio
    from nanocrawl import CrawlConfig, crawl

    stats = asyncio.run(crawl(
        CrawlConfig(seeds=["https://example.com"], max_pages=200),
        output="corpus.jsonl",
    ))
    print(stats.report())

The output is JSON Lines with a ``text`` field per page, which is what
``nanosearch build`` reads -- so a crawl becomes a search index in one more
command.

Design in one paragraph: a pool of asyncio workers pulls URLs from a
breadth-first :class:`~nanocrawl.frontier.Frontier` that is fair across hosts
and enforces a minimum delay per host. Each worker checks
:class:`~nanocrawl.robots.RobotsCache` before requesting anything, fetches with
timeouts and bounded retries, extracts text and links with a standard-library
HTML parser, and drops duplicates at three levels -- URL, exact content, and
near-duplicate via SimHash.
"""

from __future__ import annotations

from .crawler import CrawlConfig, Crawler, crawl
from .dedup import BloomFilter, ContentDeduplicator, UrlSet, hamming_distance, simhash
from .fetch import Fetcher, FetchResult
from .frontier import Frontier, Request
from .parse import ParsedPage, parse_html
from .robots import RobotsCache
from .stats import CrawlStats
from .store import Document, JsonlWriter, read_corpus
from .urls import UrlFilter, normalise, same_site

__version__ = "0.1.0"

__all__ = [
    "BloomFilter",
    "ContentDeduplicator",
    "CrawlConfig",
    "CrawlStats",
    "Crawler",
    "Document",
    "FetchResult",
    "Fetcher",
    "Frontier",
    "JsonlWriter",
    "ParsedPage",
    "Request",
    "RobotsCache",
    "UrlFilter",
    "UrlSet",
    "__version__",
    "crawl",
    "hamming_distance",
    "normalise",
    "parse_html",
    "read_corpus",
    "same_site",
    "simhash",
]
