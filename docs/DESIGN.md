# Design notes

Why the crawler is shaped the way it is, what the benchmarks revealed, and
what it deliberately does not do.

---

## 1. The problem

A crawler is a loop: take a URL, fetch it, extract links, repeat. Every hard
part is in the words "repeat" and "fetch".

**It is almost entirely waiting.** A page takes 20–500 ms to come back and
under a millisecond to parse, so a crawler that fetches one page at a time is
idle more than 99% of the time. Concurrency is not an optimisation here; it is
the whole design.

**The input is adversarial, without anyone intending it.** Real sites contain:

| Hazard | What happens without a defence | Defence |
|---|---|---|
| Cycles (A links to B links to A) | never terminates | normalised URL set |
| The same page at several URLs | the same work several times | URL normalisation |
| Infinite URL spaces (`?day=N`) | never terminates, and no deduplication helps — every URL really is different | depth and page budgets |
| Malformed HTML | a parser exception ends the crawl | a tolerant parser |
| A page slower than forever | a worker is lost permanently | timeouts |
| A 2 GB "page" | memory exhaustion | streamed, size-capped bodies |
| Near-duplicate pages | a corpus of the same article fifteen times | SimHash |

**Going fast is indistinguishable from an attack.** A crawler can issue
requests orders of magnitude faster than a small site can serve them. This is
the constraint that shapes everything else, and it is why the rate limiter is
not a feature but a core component.

---

## 2. The scheduler

Two jobs that are usually conflated:

* **Order** — which URL is next. A queue: first in, first out, which is
  breadth-first. Breadth-first is right for a crawler because pages near the
  seed are usually the ones that matter, so a crawl cut short still got the
  important pages. Depth-first wanders into one branch and never returns.
* **Timing** — when it may be fetched. A per-host minimum interval.

### Fairness across hosts

URLs are kept in one queue per host and popped round-robin. With a single-host
crawl this costs nothing; with several seeds it is the difference between a
crawl that finishes and one that spends an hour on the first domain it met.

### Slot reservation

The obvious rate limiter is a lock per host held for the duration of the
delay. That serialises workers behind a sleeping lock-holder and scales badly.
Instead each worker *reserves* the next free moment and sleeps until it:

```python
async with lock:  # microseconds
    earliest = max(now, next_allowed[host])
    next_allowed[host] = earliest + delay
    wait = earliest - now
await asyncio.sleep(wait)  # outside the lock
```

Ten workers hitting one host get ten distinct slots spaced `delay` apart. No
worker holds a lock while sleeping, and throughput settles at exactly
`1/delay` per host however many workers run. **Measured: 10.2 pages/s at a
0.1 s limit, identical at 4 and at 16 workers, with the smallest observed gap
at the server being 98 ms.**

### Termination

The classic crawler bug. A worker that exits when the queue is empty will
exit while another worker is mid-fetch and about to discover fifty more links;
the crawl then stops silently, half done, and reports success.

The crawl is finished when **nothing is queued and nothing is in flight**.
Both counters live in the frontier under one condition variable, and
`frontier.done()` is called in a `finally` on every path — a missed decrement
means the frontier believes work is still in flight and no worker ever exits,
which is a deadlock rather than a wrong answer.

Four tests exist only for this: that an empty queue with work in flight makes
a worker wait rather than exit, that every worker exits when the work really
does run out, that mid-crawl discovery keeps workers alive, and that an early
stop wakes all of them.

---

## 3. Duplicates at three levels

Duplicates arrive at three resolutions and each needs a different structure.

**The same URL.** A set of normalised URLs. Exact, cheap, and the first line
of defence: most duplicate work never happens because the URL was already
queued.

Normalisation is where this is won or lost. These are all one page:

```
HTTP://Example.com:80/a/../b?y=2&x=1#frag
http://example.com/b?x=1&y=2
http://example.com/./a/c/../../b?y=2&x=1
```

Scheme and host lowercased, default port dropped, fragment removed, dot
segments resolved, query parameters sorted, percent-escapes that were never
needed decoded. Every rule is one RFC 3986 says is safe; stripping a trailing
slash is *not* safe — servers may serve different content at `/a` and `/a/` —
so it is offered and left off.

**Byte-identical content at different URLs.** SHA-256 of the whitespace-
normalised text.

**Nearly identical content.** The expensive case: the same article with a
different sidebar, a print view, page 2 repeating the header. Exact hashing
cannot see these because one byte differs.

SimHash is a hash built to do the opposite of what hashes usually do. Break
the text into overlapping 4-word shingles, hash each to 64 bits, and for every
bit position add a vote: +1 if that shingle's bit is set, −1 if not. The
fingerprint's bit is 1 where the votes ended up positive. Because each bit is
a vote across every shingle, changing a few shingles moves few votes across
zero and the fingerprint barely changes.

### Choosing the threshold — and why the textbook value is wrong here

The literature's value for 64-bit fingerprints is 3 bits. Measured on this
project's own corpora it catches **half** of real near-duplicates, because the
threshold is length-sensitive and the documents in that paper are much longer
than a typical page. The same one-sentence edit, by document length:

| document | median distance | p90 | max |
|---|---|---|---|
| ~75 words | 6 | 10 | 10 |
| ~150 words | 4 | 7 | 10 |
| ~325 words | 2 | 6 | 8 |
| ~800 words | 2 | 4 | 5 |

And across 450 near-duplicate pairs against 150 unrelated ones:

| threshold | caught | false positives | bands needed |
|---|---|---|---|
| 3 | 50.0% | 0.0% | 4 |
| 5 | 67.1% | 0.0% | 6 |
| **7** | **80.0%** | **0.0%** | **8** |
| 9 | 87.8% | 0.0% | 10 |
| 12 | 97.3% | 0.0% | 13 |

Near-duplicates never exceeded 16 bits; unrelated documents never came closer
than 24. There is a wide empty gap, so no threshold in this range produces a
false positive — 12 looks strictly better than 7.

The reason it is not is the lookup.

### Banding, and why it caps the threshold

Comparing each new fingerprint against every stored one is O(n) per page and
O(n²) over a crawl. Instead the 64-bit fingerprint is cut into equal bands and
each band indexed, so only pages sharing a whole band are ever compared.

This is exact, not heuristic: **if two fingerprints differ in at most `k` bits
and there are more than `k` bands, at least one band must be identical** — the
pigeonhole principle. A test asserts the banded index finds exactly what brute
force finds.

The guarantee needs `bands > threshold`, and bands must divide 64. A threshold
of 12 forces 16 bands of 4 bits — keys with only 16 possible values, so every
bucket collides and the index stops narrowing anything. **7 with 8 bands is
the most sensitive setting that keeps both the guarantee and selective keys**,
and that is the default.

### The Bloom filter

For the URL set, optional and off by default:

| URLs | Bloom | set (estimate) | bytes/URL | measured error |
|---|---|---|---|---|
| 10,000 | 0.01 MB | 1.0 MB | 1.20 | 0.90% |
| 1,000,000 | 1.20 MB | 100 MB | 1.20 | — |

~80× smaller, with no false negatives — a URL it has never seen is never
reported as seen. The cost is the reverse: about 1% of pages are skipped that
should have been fetched. Worth it at a hundred million URLs, not at ten
thousand, which is why the exact set is the default.

---

## 4. Order of operations

Inside one page, and not obvious:

1. **robots.txt first**, before anything is requested from the host.
2. **Politeness wait second**, before the fetch. Waiting afterwards protects
   nobody.
3. **Links queued before the page is filtered.** A page can be too short to
   store, or ask not to be indexed, and still be the only route to the rest of
   the site. The test site has a page reachable only through a `noindex` page,
   precisely to keep this honest.
4. **Storage last**, once the page has survived every filter.

---

## 5. Two bugs the benchmarks found

Both are in the repository's history and both are now regression-tested.

**The page budget did not hold.** `max_pages` was checked after a page was
successfully stored. On a site where every page is a duplicate, nothing is
ever stored — so the check never ran and the crawl continued until the
frontier emptied. A budget that silently fails is worse than no budget. The
check now lives in a `finally` and counts *fetches*, which is what it always
meant.

This surfaced because a throughput benchmark returned 125 pages when asked for
25. The benchmark was also wrong — its "unique" pages differed by a serial
number in 220 words, which SimHash correctly called near-duplicates — so one
run exposed a bug in the crawler *and* a bug in the measurement.

**An early stop discarded the work a checkpoint needed.** Stopping at the page
budget cleared the frontier's queues to wake the workers. That also threw away
every queued URL, so the checkpoint written immediately afterwards had nothing
to resume from. `close()` now only sets the finished flag, and `get()` checks
that flag before the queue.

---

## 6. Testing strategy

160 tests, 96% coverage, against a real HTTP server on a real socket. A mocked
transport would pass while the real thing failed on redirects, chunked
encoding, connection reuse or content-length — which is most of what a crawler
has to get right.

The tests that earn their place:

- **robots-disallowed paths are never requested**, asserted against the
  server's own request log rather than the crawler's counters
- **the delay is honoured on the wire**, from request timestamps at the server
- **sixteen workers do not beat the rate limit**
- **every worker exits; none exits early**
- **every URL is fetched at most once** despite a site full of cycles
- **links are still followed from pages that are not stored**
- **banded near-duplicate lookup equals brute force**
- **`links_found == links_queued + skipped`**, so a new skip path that forgets
  its counter fails the build instead of quietly losing pages

---

## 7. What this does not do

- **No JavaScript.** Pages rendered client-side come back empty. Fixing this
  means driving a headless browser, which costs roughly 100× the memory and
  time per page — a different kind of program.
- **No sitemap.xml.** They are parsed out of robots.txt and then ignored.
  Reading them would find pages that are linked from nowhere, which is a real
  coverage gap.
- **No conditional requests.** No `If-Modified-Since` or `ETag`, so a re-crawl
  re-downloads everything. Storing validators alongside the corpus would make
  incremental crawls nearly free, and is the most valuable missing feature.
- **One process, in-memory frontier.** A distributed crawl needs the frontier
  and the seen-set in a shared store (Redis, or a partitioned queue), with
  hosts sharded consistently so politeness is still enforceable per host — you
  cannot rate-limit a host from two machines that do not talk to each other.
- **The seen-set is unbounded.** At a hundred million URLs the Bloom filter
  helps; past that it needs to live on disk.
- **No per-host adaptive rate limiting.** A fixed delay is set for the slowest
  plausible server. Production crawlers watch response times and back off
  automatically when a host slows down.
- **A page budget overshoots slightly.** Workers already in flight when the
  budget is reached still finish, so the real cap is `max_pages + concurrency`
  in the worst case.

---

## 8. References

- RFC 9309, *Robots Exclusion Protocol* — the status-code handling in
  `robots.py` follows it, including failing closed on 5xx.
- RFC 3986, *URI Generic Syntax*, sections 5.2.4 and 6.2 — the normalisation
  rules.
- Manku, Jain & Das Sarma (2007), *Detecting Near-Duplicates for Web Crawling*
  — SimHash and the banding trick.
- Bloom (1970), *Space/time trade-offs in hash coding with allowable errors*.
- Kirsch & Mitzenmacher (2006), *Less Hashing, Same Performance* — the
  two-hash trick in `BloomFilter._positions`.
