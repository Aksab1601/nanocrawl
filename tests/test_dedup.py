"""The three levels of duplicate detection."""

from __future__ import annotations

import random

import pytest

from nanocrawl.dedup import (
    BloomFilter,
    ContentDeduplicator,
    UrlSet,
    content_hash,
    hamming_distance,
    simhash,
)

WORDS = [
    "search",
    "index",
    "crawler",
    "frontier",
    "queue",
    "hash",
    "vector",
    "corpus",
    "parser",
    "token",
    "document",
]


def document(rng: random.Random, sentences: int = 13, words: int = 25) -> str:
    return " ".join(
        " ".join(rng.choice(WORDS) for _ in range(words)).capitalize() + "."
        for _ in range(sentences)
    )


class TestUrlSet:
    def test_reports_whether_a_url_is_new(self) -> None:
        seen = UrlSet()
        assert seen.add("https://a.com/1") is True
        assert seen.add("https://a.com/1") is False
        assert len(seen) == 1

    def test_membership_and_snapshot(self) -> None:
        seen = UrlSet()
        seen.add("https://a.com/1")
        assert "https://a.com/1" in seen
        assert seen.snapshot() == ["https://a.com/1"]


class TestBloomFilter:
    def test_never_has_false_negatives(self) -> None:
        """The property that makes it safe to use here at all."""
        bloom = BloomFilter(expected_items=2000, error_rate=0.01)
        added = [f"https://example.com/page/{i}" for i in range(2000)]
        for url in added:
            bloom.add(url)
        assert all(url in bloom for url in added)

    def test_false_positive_rate_is_near_the_target(self) -> None:
        bloom = BloomFilter(expected_items=5000, error_rate=0.01)
        for i in range(5000):
            bloom.add(f"https://example.com/in/{i}")
        false_positives = sum(1 for i in range(5000) if f"https://example.com/out/{i}" in bloom)
        rate = false_positives / 5000
        # Generous bound: the point is that it is ~1%, not 10% or 0.01%.
        assert rate < 0.03, rate
        assert bloom.estimated_error_rate() < 0.03

    def test_uses_far_less_memory_than_a_set(self) -> None:
        count = 100_000
        bloom = BloomFilter(expected_items=count, error_rate=0.01)
        bytes_per_url = bloom.memory_bytes / count
        assert bytes_per_url < 2.0, bytes_per_url

    def test_add_reports_novelty(self) -> None:
        bloom = BloomFilter(expected_items=100)
        assert bloom.add("https://a.com/1") is True
        assert bloom.add("https://a.com/1") is False
        assert len(bloom) == 1

    def test_rejects_nonsense_parameters(self) -> None:
        with pytest.raises(ValueError, match="expected_items"):
            BloomFilter(expected_items=0)
        with pytest.raises(ValueError, match="error_rate"):
            BloomFilter(expected_items=10, error_rate=1.5)


class TestContentHash:
    def test_ignores_whitespace_differences(self) -> None:
        assert content_hash("a  b\n c") == content_hash("a b c")

    def test_differs_for_different_text(self) -> None:
        assert content_hash("a b c") != content_hash("a b d")


class TestSimhash:
    def test_identical_text_has_distance_zero(self) -> None:
        rng = random.Random(0)
        text = document(rng)
        assert hamming_distance(simhash(text), simhash(text)) == 0

    def test_small_edits_stay_close(self) -> None:
        rng = random.Random(1)
        base = document(rng)
        edited = base + " One short sentence added at the end."
        assert hamming_distance(simhash(base), simhash(edited)) <= 12

    def test_unrelated_documents_are_far_apart(self) -> None:
        rng = random.Random(2)
        a, b = document(rng), document(rng)
        assert hamming_distance(simhash(a), simhash(b)) >= 20

    def test_near_and_unrelated_distributions_do_not_overlap(self) -> None:
        """The property a threshold depends on.

        If the two distributions overlapped there would be no safe threshold,
        and near-duplicate detection would be guesswork. Measured over many
        pairs so one unlucky document cannot pass it by chance.
        """
        rng = random.Random(3)
        near, unrelated = [], []
        for _ in range(40):
            base = document(rng)
            near.append(hamming_distance(simhash(base), simhash(base + " Updated in March.")))
            unrelated.append(hamming_distance(simhash(base), simhash(document(rng))))
        assert max(near) < min(unrelated), (max(near), min(unrelated))

    def test_empty_text_is_handled(self) -> None:
        assert simhash("") == 0
        assert simhash("   ") == 0

    def test_short_text_does_not_crash(self) -> None:
        assert simhash("two words") >= 0


class TestContentDeduplicator:
    def test_detects_an_exact_duplicate(self) -> None:
        rng = random.Random(4)
        text = document(rng)
        dedup = ContentDeduplicator()
        assert dedup.check(text, "https://a.com/1") is None
        assert dedup.check(text, "https://a.com/2") == "https://a.com/1"
        assert dedup.exact_hits == 1

    def test_detects_a_near_duplicate(self) -> None:
        rng = random.Random(5)
        text = document(rng)
        dedup = ContentDeduplicator()
        dedup.check(text, "https://a.com/1")
        found = dedup.check(text + " A sentence added at the end.", "https://a.com/2")
        assert found == "https://a.com/1"
        assert dedup.near_hits == 1

    def test_keeps_genuinely_different_pages(self) -> None:
        rng = random.Random(6)
        dedup = ContentDeduplicator()
        for i in range(25):
            assert dedup.check(document(rng), f"https://a.com/{i}") is None
        assert dedup.unique_pages == 25

    def test_banding_finds_every_pair_within_the_threshold(self) -> None:
        """The pigeonhole guarantee, checked against brute force.

        Banding only narrows the search if it never misses a pair that a full
        scan would have found. This asserts exactly that equivalence.
        """
        rng = random.Random(7)
        dedup = ContentDeduplicator(max_distance=7, bands=8)
        stored: list[tuple[int, str]] = []
        misses = 0

        for i in range(60):
            text = document(rng) if i % 3 else document(rng) + " tail sentence here."
            fingerprint = simhash(text)
            brute_force = any(hamming_distance(fingerprint, other) <= 7 for other, _ in stored)
            banded = dedup.check(text, f"https://a.com/{i}") is not None
            if brute_force != banded:
                misses += 1
            if not banded:
                stored.append((fingerprint, f"https://a.com/{i}"))

        assert misses == 0

    def test_rejects_settings_that_break_the_guarantee(self) -> None:
        with pytest.raises(ValueError, match="must exceed max_distance"):
            ContentDeduplicator(max_distance=8, bands=8)
        with pytest.raises(ValueError, match="must divide 64"):
            ContentDeduplicator(max_distance=3, bands=5)
