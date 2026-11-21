"""How much does concurrency buy, and what does politeness cost?

Run::

    python benchmarks/bench_crawl.py
    python benchmarks/bench_crawl.py --pages 400 --page-delay 0.05

Method
------
Everything is measured against a local site generated for the purpose, with a
fixed artificial delay per page. That is deliberate: a crawler's throughput is
set almost entirely by how long other people's servers take, so a benchmark
against the real web measures the web, not the crawler. Fixing the server
latency makes the one variable the crawler's own scheduling.

Two questions:

1. **Does concurrency work?** With no rate limit, ``n`` workers against a
   server that takes ``d`` seconds per page should approach ``n/d`` pages per
   second until something else becomes the bottleneck.
2. **Does politeness hold?** With a per-host delay, throughput must flatten at
   ``1/delay`` no matter how many workers are added -- that is the whole point
   of the rate limiter, and the number to check is the one measured at the
   server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanocrawl.crawler import CrawlConfig, Crawler  # noqa: E402
from nanocrawl.testing import SitePage, SiteServer  # noqa: E402

WORDS = "crawler frontier queue scheduler parser corpus index latency throughput worker request response document retrieval memory network protocol".split()  # noqa: SIM905 - prose beats a list literal


def flat_site(pages: int, page_delay: float, words: int = 220) -> dict[str, SitePage]:
    """A site of ``pages`` leaf pages, all linked from one hub.

    A flat shape on purpose: a deep tree would measure how fast the crawler
    *discovers* links, which varies with the shape of the site, rather than
    how fast it fetches.
    """
    import random

    def body_for(seed: int) -> str:
        # Genuinely different text per page. Reusing one body with a serial
        # number appended is not enough: SimHash correctly calls those
        # near-duplicates, and the benchmark would measure deduplication.
        rng = random.Random(seed)
        return " ".join(rng.choice(WORDS) for _ in range(words))

    body = body_for(0)
    links = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(pages))
    site = {
        "/robots.txt": SitePage(
            "User-agent: *\nAllow: /\n", content_type="text/plain; charset=utf-8"
        ),
        "/": SitePage(f"<main><p>{body}</p>{links}</main>", delay=page_delay),
    }
    for i in range(pages):
        site[f"/p{i}"] = SitePage(
            f"<main><p>page {i} {body_for(i + 1)}</p></main>", delay=page_delay
        )
    return site


async def run_once(
    pages: dict[str, SitePage], concurrency: int, delay: float, max_pages: int
) -> dict[str, Any]:
    with SiteServer(pages=pages) as server:
        config = CrawlConfig(
            seeds=[server.url("/")],
            max_pages=max_pages,
            max_depth=2,
            concurrency=concurrency,
            delay=delay,
            timeout=30.0,
            min_words=20,
        )
        started = time.monotonic()
        stats = await Crawler(config).run()
        elapsed = time.monotonic() - started

        # Measured at the server, which is the only account that matters for
        # politeness: the crawler's own numbers could be wrong.
        moments = [m for m, path in server.timeline if not path.startswith("/robots")]
        gaps = [b - a for a, b in zip(moments, moments[1:], strict=False)]

    return {
        "concurrency": concurrency,
        "delay": delay,
        "pages": stats.pages_fetched,
        "elapsed_s": round(elapsed, 2),
        "pages_per_second": round(stats.pages_fetched / elapsed, 2),
        "fetch_p50_ms": stats.percentile(50),
        "fetch_p95_ms": stats.percentile(95),
        "min_gap_ms": round(min(gaps) * 1000, 1) if gaps else 0.0,
    }


async def main_async(args: argparse.Namespace) -> int:
    pages = flat_site(args.pages, args.page_delay)
    budget = args.pages + 1
    results: list[dict[str, Any]] = []

    print(
        f"\n{args.pages} pages, {args.page_delay * 1000:.0f} ms server latency each, "
        f"no rate limit\n"
    )
    header = (
        f"{'workers':>8} {'pages':>6} {'elapsed':>8} {'pages/s':>8} {'speedup':>8} {'p95 ms':>7}"
    )
    print(header)
    print("-" * len(header))

    baseline = None
    for concurrency in args.concurrency:
        row = await run_once(pages, concurrency, 0.0, budget)
        baseline = baseline or row["pages_per_second"]
        row["speedup"] = round(row["pages_per_second"] / baseline, 2)
        results.append(row)
        print(
            f"{row['concurrency']:>8} {row['pages']:>6} {row['elapsed_s']:>7.2f}s "
            f"{row['pages_per_second']:>8.2f} {row['speedup']:>7.2f}x {row['fetch_p95_ms']:>7.0f}"
        )

    # A smaller site for the rate-limited runs, crawled to completion: a
    # truncated crawl's elapsed time includes workers waiting on slots they
    # never used, which flatters nothing and measures less.
    small = flat_site(args.polite_pages - 1, args.page_delay)
    print(f"\nwith a {args.delay}s per-host rate limit (the default is 0.5s)\n")
    header = f"{'workers':>8} {'pages':>6} {'elapsed':>8} {'pages/s':>8} {'min gap':>9}"
    print(header)
    print("-" * len(header))
    for concurrency in args.polite_concurrency:
        row = await run_once(small, concurrency, args.delay, args.polite_pages)
        results.append(row)
        print(
            f"{row['concurrency']:>8} {row['pages']:>6} {row['elapsed_s']:>7.2f}s "
            f"{row['pages_per_second']:>8.2f} {row['min_gap_ms']:>8.0f}ms"
        )
    print(
        f"\n  throughput is capped at ~{1 / args.delay:.1f} pages/s however many workers "
        f"run, and no two requests to the host ever land closer than the delay."
    )

    out = Path(args.out)
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    payload = {
        "meta": {
            "pages": args.pages,
            "page_delay_s": args.page_delay,
            "rate_limit_s": args.delay,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "rows": results,
    }
    await asyncio.to_thread(
        (out / "crawl_throughput.json").write_text, json.dumps(payload, indent=2)
    )
    print(f"\nwrote {out / 'crawl_throughput.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=200)
    parser.add_argument("--page-delay", type=float, default=0.02, help="server latency per page")
    parser.add_argument("--concurrency", type=int, nargs="*", default=[1, 2, 4, 8, 16, 32])
    parser.add_argument("--delay", type=float, default=0.1, help="per-host rate limit to test")
    parser.add_argument("--polite-concurrency", type=int, nargs="*", default=[4, 16])
    parser.add_argument("--polite-pages", type=int, default=25)
    parser.add_argument("--out", default="docs/bench")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
