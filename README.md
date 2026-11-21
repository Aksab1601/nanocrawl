# nanocrawl

**A polite, concurrent web crawler that turns a website into a clean text corpus. One dependency.**

[![CI](https://github.com/Aksab1601/nanocrawl/actions/workflows/ci.yml/badge.svg)](https://github.com/Aksab1601/nanocrawl/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Tests](https://img.shields.io/badge/tests-pytest-blue)
![Coverage](https://img.shields.io/badge/CI%20coverage-%E2%89%A590%25-blue)
![License](https://img.shields.io/badge/license-MIT-green)

"Design a web crawler" is a standard systems interview question because it is
deceptively hard: the work is massively I/O-bound, the input is adversarial
(cycles, infinite URL spaces, malformed HTML), and going fast is the same thing
as attacking somebody's server. This is a working answer — an asyncio worker
pool over a breadth-first frontier that is fair across hosts, rate-limited per
host, and honest about every URL it decides not to fetch.

Output is JSON Lines that [`nanosearch`](https://github.com/Aksab1601/nanosearch)
indexes directly, so a crawl becomes a working search engine in one more
command.

```bash
pip install -e .
nanocrawl --demo              # crawls a bundled fake site, no network needed
```

---

## Results

Measured against a local server with a fixed artificial latency per page, so
the only variable is the crawler's own scheduling. Reproduce with
`make bench`.

**Concurrency works.** 201 requests (the seed page plus 200 linked pages),
20 ms server latency each, no rate limit. Results below are transcribed
from [`docs/bench/crawl_throughput.json`](docs/bench/crawl_throughput.json):

| workers | elapsed | pages/s | speedup | fetch p95 |
|---|---|---|---|---|
| 1 | 5.11 s | 39.32 | 1.00× | 23.22 ms |
| 2 | 2.91 s | 69.18 | 1.76× | 29.26 ms |
| 4 | 1.70 s | 118.44 | 3.01× | 37.04 ms |
| **8** | **1.23 s** | **163.38** | **4.16×** | 56.19 ms |
| 16 | 1.35 s | 148.75 | 3.78× | 76.14 ms |
| 32 | 1.70 s | 118.17 | 3.01× | 1057.25 ms |

It flattens after 8 workers because the *test server* saturates — two cores
serving a thread per request — not the crawler. The p95 at 32 workers is the
queue forming on the server side, which is exactly what a rate limit exists to
prevent in the real world.

**Politeness holds.** The same site with a 0.1 s per-host limit:

| workers | elapsed | pages/s | smallest gap between requests |
|---|---|---|---|
| 4 | 2.44 s | 10.23 | 96.9 ms |
| 16 | 2.45 s | 10.22 | 96.8 ms |

Nearly identical. Four workers or sixteen, throughput stays close to
`1/delay` (10 requests/s). The shortest server-observed gaps were about
97 ms for a configured 100 ms delay; timing variation means this measurement
does **not** establish a strict 100 ms minimum at the server.

**Duplicate detection.** 450 near-duplicate pairs against 150 unrelated ones:

| SimHash threshold | near-duplicates caught | false positives |
|---|---|---|
| 3 (the literature's value) | 50.0% | 0.0% |
| **7 (this default)** | **80.0%** | **0.0%** |
| 12 | 97.3% | 0.0% |

Near-duplicates never exceeded 16 bits apart; unrelated documents never came
closer than 24. The reason not to simply take 12 is in
[the design notes](docs/DESIGN.md#duplicates-at-three-levels) — it would break
the pigeonhole guarantee that makes the lookup sublinear.

---

## Quickstart

```bash
git clone https://github.com/Aksab1601/nanocrawl.git
cd nanocrawl
pip install -e ".[dev]"

nanocrawl --demo                       # a fake site on localhost, 10 seconds
make check                             # lint, type check, 160 tests
make bench                             # the numbers above
```

**Windows (Git Bash or PowerShell):** If `make` is not installed, run
`python -m pytest -q --cov=nanocrawl --cov-report=term-missing`,
`python -m ruff check .`, and `python -m mypy` instead of `make check`.
The local demo works with `python -m nanocrawl.cli --demo`.

Crawl something real:

```bash
nanocrawl https://example.com --out corpus.jsonl --max-pages 500
nanocrawl https://docs.example.com --allow-prefix /guide --delay 1.0
```

Then search it:

```bash
nanosearch build corpus.jsonl --out ./index
nanosearch search ./index "how do I configure logging"
```

As a library:

```python
import asyncio
from nanocrawl import CrawlConfig, crawl

stats = asyncio.run(
    crawl(
        CrawlConfig(seeds=["https://example.com"], max_pages=200, delay=1.0),
        output="corpus.jsonl",
    )
)
print(stats.report())
```

---

## Politeness is not configurable

**There is no flag to ignore robots.txt.** It is checked before the first
request to a host, cached for the crawl, and obeyed. The flag would be used
once, in a hurry, and then forgotten — and a crawler that ignores robots.txt
is the difference between a tool and a nuisance.

The rest of the defaults are deliberately timid: one request per host every
half second, 500 pages, three links deep, same site only. A crawler's defaults
are a statement about who pays for a mistake, and it is not the person running
it.

Behaviour follows RFC 9309, including the unglamorous parts:

| robots.txt returns | what happens |
|---|---|
| 2xx | rules parsed and applied, `Crawl-delay` honoured when slower than ours |
| 404 / 410 | no file, no restrictions |
| 401 / 403 | the rules themselves are forbidden → the whole site is off limits |
| 5xx or unreachable | **fail closed** — a site having a bad day is not permission |

Also honoured: `<meta name="robots" content="noindex">` (fetched and followed,
never stored), `rel="nofollow"` links, and `Retry-After` on 429s. Retries are
floored at the host's delay, so the one request that happens when a server is
already struggling is not the one that ignores the rate limit.

---

## How it works

```
seeds ──► Frontier ──► workers ──► robots? ──► wait for this host's turn
            ▲                          │
            │                          ▼
            └──── links ◄── parse ◄── fetch ──► dedup ──► corpus.jsonl
```

**The frontier** is a breadth-first queue, kept per host and popped
round-robin so one big site cannot starve a small one. Politeness is separate
from ordering: a worker *reserves* the next free moment for a host under a
lock and then sleeps until it, so ten workers on one host get ten slots spaced
`delay` apart, nobody holds a lock while sleeping, and throughput settles at
exactly `1/delay` regardless of worker count.

**Termination** is the classic crawler bug. The crawl is finished when nothing
is queued *and* nothing is in flight — an empty queue alone proves nothing,
because a worker still fetching may be about to discover fifty more links.
Several tests exist only to prove no worker exits early and none deadlocks.

**Parsing** is `html.parser` from the standard library, subclassed. It drops
`<script>`, `<style>` and friends entirely, drops `<nav>`/`<header>`/
`<footer>`/`<aside>` as boilerplate, and prefers `<main>` or `<article>` when
the page declares one — because without that, every page in the corpus carries
the same navigation menu and they all look alike to a search index.

**Duplicates** are caught at three levels, because they arrive at three:
the same URL (a set of normalised URLs, or a Bloom filter at 1.2 bytes each),
different URLs with identical text (SHA-256), and different URLs with *nearly*
identical text (SimHash, with banded lookup so it stays sublinear).

**URL normalisation** decides whether a crawl terminates at all.
`HTTP://Example.com:80/a/../b?y=2&x=1#frag` and `http://example.com/b?x=1&y=2`
are the same page, and a crawler that disagrees fetches it twice — or, on a
site with a calendar widget, forever.

**[→ Full design notes](docs/DESIGN.md)** — the scheduler, the termination
argument, the three deduplication levels, what the benchmarks revealed, and an
explicit list of what this does not do.

---

## Repository layout

```
nanocrawl/
├── urls.py        normalisation (RFC 3986) and scope filters
├── frontier.py    BFS queue, per-host fairness, slot reservation
├── robots.py      robots.txt: fetch, cache, fail closed
├── fetch.py       timeouts, retries with jitter, size caps
├── parse.py       stdlib HTML parser: links, text, boilerplate removal
├── dedup.py       URL set, Bloom filter, SHA-256, SimHash + banding
├── crawler.py     the worker pool and the order of operations
├── stats.py       counters for every decision, including the skips
├── store.py       JSONL corpus, flushed per page
├── testing.py     a fake website with every hazard built in
└── cli.py
tests/             160 tests against a real HTTP server
benchmarks/        throughput, politeness, deduplication
docs/DESIGN.md     design notes
```

## Engineering notes

- **Tested against a real server**, not a mocked transport: redirects, chunked
  encoding, connection reuse and content-length are most of what a crawler has
  to get right, and a mock passes while the real thing fails.
- **The test site contains every hazard on purpose** — cycles, duplicate and
  near-duplicate pages, an infinite `?day=N` parameter trap, a 404, a 500, a
  redirect chain, a page slower than the timeout, a robots-disallowed section,
  `noindex` and `nofollow`. Determinism from a seed means the same crawl
  produces the same corpus every run.
- **Every skipped link is counted by reason**, and a test asserts
  `links_found == links_queued + skipped`. A crawler that silently drops 90%
  of what it finds is impossible to debug.
- **Politeness is verified at the server**, from request timestamps, rather
  than from the crawler's own accounting.
- **Two bugs the benchmarks found**, both documented in the design notes: an
  early stop discarded the queue a checkpoint needed, and the page budget was
  checked only after a successful store — so on a site of duplicates it was
  silently ignored.

## What's missing

No JavaScript rendering, no sitemap.xml parsing, no distributed frontier, no
persistent URL store, no `Accept-Language` or conditional-request support. Each
is explained, with what it would take, in
[§7 of the design notes](docs/DESIGN.md#7-what-this-does-not-do).

## License

MIT
