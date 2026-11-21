# Benchmark artifacts

Committed so the README's numbers can be checked rather than taken on trust.

| File | What it is |
|---|---|
| `crawl_throughput.json` | Pages/s against worker count with no rate limit, then the same site under a 0.1 s per-host limit. The `min_gap_ms` column is measured at the server from request timestamps, not from the crawler's own counters. |
| `dedup.json` | SimHash distance distributions for near-duplicate and unrelated pairs, the catch rate at each threshold, and Bloom filter memory against a plain set. |
| `raw/*.log` | The console output of the runs that produced those files. |

Reproduce:

```bash
make bench        # throughput and politeness
make bench-dedup  # SimHash thresholds and Bloom memory
```

Both run entirely against a local server, so they touch no external site and
the results are deterministic apart from timing. The absolute pages/s figures
depend on the machine; the *ratios* and the politeness gaps are the claims.
This run was a 2-core container, where the test server itself becomes the
bottleneck past about 8 workers — which is why the speedup column flattens.
