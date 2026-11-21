"""Command line interface.

    nanocrawl --demo                                  # crawl the built-in test site
    nanocrawl https://example.com --out corpus.jsonl
    nanocrawl https://docs.example.com --allow-prefix /guide --max-pages 2000

``--demo`` starts the bundled test site on localhost and crawls that, so the
project can be evaluated in ten seconds without pointing it at anybody's
server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .crawler import CrawlConfig, Crawler
from .stats import CrawlStats
from .store import Document, JsonlWriter

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanocrawl",
        description="A polite, concurrent web crawler that writes a JSONL text corpus.",
        epilog="robots.txt is always respected; there is no flag to disable it.",
    )
    parser.add_argument("seeds", nargs="*", help="one or more URLs to start from")
    parser.add_argument("--demo", action="store_true", help="crawl the bundled test site")
    parser.add_argument("--out", "-o", default="corpus.jsonl", help="output JSONL path")

    scope = parser.add_argument_group("scope")
    scope.add_argument("--max-pages", type=int, default=500, help="0 means no limit")
    scope.add_argument("--max-depth", type=int, default=3)
    scope.add_argument("--allow-prefix", action="append", default=[], metavar="PATH")
    scope.add_argument("--deny-prefix", action="append", default=[], metavar="PATH")
    scope.add_argument(
        "--off-site", action="store_true", help="follow links to other sites (careful)"
    )
    scope.add_argument(
        "--no-subdomains", action="store_true", help="treat subdomains as different sites"
    )

    manners = parser.add_argument_group("politeness")
    manners.add_argument(
        "--delay", type=float, default=0.5, help="seconds between requests per host"
    )
    manners.add_argument("--concurrency", "-c", type=int, default=8)
    manners.add_argument("--timeout", type=float, default=15.0)
    manners.add_argument("--max-retries", type=int, default=2)
    manners.add_argument(
        "--user-agent",
        default="nanocrawl/0.1 (+https://github.com/)",
        help="identify yourself; include a contact URL when crawling in earnest",
    )

    content = parser.add_argument_group("content")
    content.add_argument("--min-words", type=int, default=20)
    content.add_argument("--keep-boilerplate", action="store_true", help="keep nav/header/footer")
    content.add_argument("--no-dedup", action="store_true", help="skip near-duplicate detection")
    content.add_argument("--near-distance", type=int, default=7, help="SimHash bit threshold")
    content.add_argument("--bloom", action="store_true", help="Bloom filter instead of a set")

    run = parser.add_argument_group("run")
    run.add_argument("--resume", metavar="STATE.json", help="checkpoint file to write and resume")
    run.add_argument("--stats-json", metavar="PATH", help="write the final stats as JSON")
    run.add_argument("--quiet", "-q", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace, seeds: list[str]) -> CrawlConfig:
    return CrawlConfig(
        seeds=seeds,
        max_pages=None if args.max_pages == 0 else args.max_pages,
        max_depth=args.max_depth,
        concurrency=args.concurrency,
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_retries=args.max_retries,
        same_site_only=not args.off_site,
        include_subdomains=not args.no_subdomains,
        allow_prefixes=tuple(args.allow_prefix),
        deny_prefixes=tuple(args.deny_prefix),
        drop_boilerplate=not args.keep_boilerplate,
        min_words=args.min_words,
        near_duplicates=not args.no_dedup,
        near_duplicate_distance=args.near_distance,
        use_bloom=args.bloom,
        checkpoint_path=Path(args.resume) if args.resume else None,
    )


class Progress:
    """One rewritten line of progress, at most twice a second."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._last = 0.0

    def __call__(self, document: Document, stats: CrawlStats) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last < 0.5:
            return
        self._last = now
        print(
            f"\r  {stats.pages_fetched:>5} fetched  {stats.pages_stored:>5} stored  "
            f"{stats.pages_per_second:>5.1f}/s  {document.url[:60]:<60}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def done(self) -> None:
        if self.enabled:
            print("\r" + " " * 100 + "\r", end="", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    seeds = list(args.seeds)
    server = None

    if args.demo:
        from .testing import SiteServer

        server = SiteServer().start()
        seeds = [server.url("/")]
        print(f"serving the bundled test site at {server.origin}")

    if not seeds:
        print("error: give at least one seed URL, or use --demo", file=sys.stderr)
        return 2

    config = config_from_args(args, seeds)
    progress = Progress(not args.quiet)

    try:
        if not args.quiet:
            print(f"crawling {', '.join(seeds)}")
            print(
                f"  max {config.max_pages or 'unlimited'} pages, depth {config.max_depth}, "
                f"{config.concurrency} workers, {config.delay}s between requests per host"
            )
        with JsonlWriter(args.out) as writer:
            crawler = Crawler(config, writer=writer, on_page=progress)
            stats = await crawler.run()
        progress.done()
    finally:
        if server is not None:
            server.stop()

    if not args.quiet:
        print(stats.report())
        print(f"  corpus       {args.out}")
        if stats.pages_stored:
            print(f"\nindex it with:  nanosearch build {args.out} --out ./index")

    if args.stats_json:
        # Off the event loop: a blocking write inside a coroutine stalls
        # every other task, which is the whole point of not doing it here.
        await asyncio.to_thread(
            Path(args.stats_json).write_text, json.dumps(stats.as_dict(), indent=2)
        )

    # A crawl that fetched nothing is a failure, whatever the exit path.
    return 0 if stats.pages_fetched else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted; the corpus written so far is intact", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
