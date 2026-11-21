"""How well do the duplicate detectors actually work?

Run::

    python benchmarks/bench_simhash.py

Two measurements, both of which set a default in the code rather than
decorating it afterwards.

**SimHash threshold.** The literature's value for 64-bit fingerprints is 3
bits (Manku, Jain & Das Sarma, 2007). Measured here it catches only about half
of real near-duplicates, because the threshold is length-sensitive and their
documents are far longer than a typical page. This prints the distance
distributions for near-duplicate and unrelated pairs, and the catch rate at
each threshold, which is where ``ContentDeduplicator``'s default of 7 comes
from.

**Bloom filter.** Memory per URL and the measured false-positive rate against
the target, which is what justifies ``UrlSet`` being the default and the Bloom
filter being opt-in.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanocrawl.dedup import BloomFilter, hamming_distance, simhash  # noqa: E402

WORDS = "search index crawler frontier queue hash vector corpus parser token document retrieval latency throughput cluster memory disk cache network server protocol request response header payload schema pipeline worker scheduler".split()  # noqa: SIM905 - prose beats a 30-item list literal


def sentence(rng: random.Random, words: int = 25) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words)).capitalize() + "."


def document(rng: random.Random, sentences: int = 13) -> str:
    return " ".join(sentence(rng) for _ in range(sentences))


def length_sensitivity(trials: int = 40) -> list[dict[str, Any]]:
    """The same edit, measured against documents of different lengths."""
    rows = []
    for sentences, label in ((3, "~75"), (6, "~150"), (13, "~325"), (32, "~800")):
        rng = random.Random(11)
        distances = []
        for _ in range(trials):
            base = document(rng, sentences)
            distances.append(
                hamming_distance(simhash(base), simhash(base + " " + sentence(rng, 7)))
            )
        rows.append(
            {
                "words": label,
                "median": statistics.median(distances),
                "p90": sorted(distances)[int(0.9 * len(distances))],
                "max": max(distances),
            }
        )
    return rows


def threshold_table(trials: int = 150) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Catch rate and false-positive rate at each candidate threshold."""
    rng = random.Random(7)
    near: list[int] = []
    unrelated: list[int] = []

    for _ in range(trials):
        base = document(rng)
        variants = [
            base + " Updated on the fourth of March.",  # a date line appended
            "Share this article. " + base,  # a widget prepended
            base.replace(base.split(". ")[0], sentence(rng), 1),  # one sentence rewritten
        ]
        near.extend(hamming_distance(simhash(base), simhash(v)) for v in variants)
        unrelated.append(hamming_distance(simhash(base), simhash(document(rng))))

    rows = []
    for threshold in (3, 5, 7, 9, 12, 16, 20):
        rows.append(
            {
                "threshold": threshold,
                "near_caught": sum(1 for d in near if d <= threshold) / len(near),
                "false_positives": sum(1 for d in unrelated if d <= threshold) / len(unrelated),
                "guarantee_needs_bands": threshold + 1,
            }
        )

    summary = {
        "near_pairs": len(near),
        "unrelated_pairs": len(unrelated),
        "near_median": statistics.median(near),
        "near_max": max(near),
        "unrelated_min": min(unrelated),
        "unrelated_median": statistics.median(unrelated),
    }
    return rows, summary


def bloom_table() -> list[dict[str, Any]]:
    """Memory per URL and measured error rate, against a plain set."""
    rows = []
    for count in (10_000, 100_000, 1_000_000):
        bloom = BloomFilter(expected_items=count, error_rate=0.01)
        sample = min(count, 50_000)
        for i in range(sample):
            bloom.add(f"https://example.com/page/{i}")
        false_positives = sum(1 for i in range(10_000) if f"https://example.com/other/{i}" in bloom)
        # A Python set of 60-character URLs costs roughly 100 bytes each once
        # the string object and the table slot are counted.
        rows.append(
            {
                "urls": count,
                "bloom_mb": round(bloom.memory_bytes / 1e6, 2),
                "set_mb_estimate": round(count * 100 / 1e6, 1),
                "bytes_per_url": round(bloom.memory_bytes / count, 2),
                "measured_error": false_positives / 10_000 if sample == count else None,
                "hash_functions": bloom.hashes,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/bench")
    args = parser.parse_args(argv)

    print("\nSimHash is length-sensitive: the same one-sentence edit, by document size\n")
    print(f"{'words':>8} {'median':>8} {'p90':>6} {'max':>6}")
    print("-" * 32)
    lengths = length_sensitivity()
    for row in lengths:
        print(f"{row['words']:>8} {row['median']:>8.1f} {row['p90']:>6} {row['max']:>6}")
    print("  a short page needs a far looser threshold than a long one for the same edit")

    rows, summary = threshold_table()
    print(
        f"\nNear-duplicate vs unrelated pairs ({summary['near_pairs']} and "
        f"{summary['unrelated_pairs']} pairs, ~325-word documents)\n"
    )
    print(f"  near-duplicates: median {summary['near_median']:.0f} bits, max {summary['near_max']}")
    print(
        f"  unrelated:       median {summary['unrelated_median']:.0f} bits, "
        f"min {summary['unrelated_min']}"
    )
    print(
        f"  the gap between {summary['near_max']} and {summary['unrelated_min']} "
        "is why a threshold exists at all\n"
    )
    print(f"{'threshold':>10} {'near caught':>12} {'false pos':>10} {'needs bands':>12}")
    print("-" * 48)
    for row in rows:
        marker = "  <- default" if row["threshold"] == 7 else ""
        print(
            f"{row['threshold']:>10} {row['near_caught']:>11.1%} "
            f"{row['false_positives']:>10.1%} {row['guarantee_needs_bands']:>12}{marker}"
        )
    print(
        "\n  bands must exceed the threshold for the pigeonhole guarantee and must\n"
        "  divide 64, so 7 (8 bands of 8 bits) is the most sensitive setting that\n"
        "  keeps both the guarantee and selective bucket keys."
    )

    print("\nBloom filter against a plain set\n")
    print(f"{'urls':>10} {'bloom':>9} {'set (est)':>11} {'bytes/url':>11} {'error':>8}")
    print("-" * 52)
    blooms = bloom_table()
    for row in blooms:
        error = f"{row['measured_error']:.2%}" if row["measured_error"] is not None else "-"
        print(
            f"{row['urls']:>10,} {row['bloom_mb']:>8.2f}M {row['set_mb_estimate']:>10.1f}M "
            f"{row['bytes_per_url']:>11.2f} {error:>8}"
        )
    print(
        "\n  ~80x smaller, at a 1% chance of skipping a page that was never crawled.\n"
        "  Worth it at a hundred million URLs; not at ten thousand, which is why\n"
        "  the exact set is the default."
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dedup.json").write_text(
        json.dumps(
            {
                "length_sensitivity": lengths,
                "thresholds": rows,
                "summary": summary,
                "bloom": blooms,
            },
            indent=2,
        )
    )
    print(f"\nwrote {out / 'dedup.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
