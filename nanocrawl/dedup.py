"""Three ways to avoid doing the same work twice.

A crawler meets duplicates at three different levels, and each needs a
different structure:

1. **The same URL, seen again.** Solved by a set of normalised URLs. Exact,
   cheap, and the first line of defence -- most duplicate work never happens
   because the URL was already queued.
2. **Different URLs, byte-identical content.** ``/about`` and
   ``/about/index.html``; a page reachable by three menu paths. Solved by
   hashing the extracted text.
3. **Different URLs, nearly identical content.** The same article with a
   different sidebar, a "print view", page 2 of a paginated list that repeats
   the header. These are the expensive ones -- they defeat exact hashing
   because one byte differs -- and they are what SimHash is for.

All three are in one module because they are one question asked at three
resolutions.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

__all__ = [
    "BloomFilter",
    "ContentDeduplicator",
    "UrlSet",
    "content_hash",
    "hamming_distance",
    "simhash",
]

_WORD = re.compile(r"\w+")


# --------------------------------------------------------------------- URLs


class UrlSet:
    """An exact set of seen URLs.

    The right default. A million URLs averaging 60 characters costs roughly
    100 MB in a Python set, which is fine; past that, a Bloom filter trades
    exactness for a constant, much smaller footprint.
    """

    __slots__ = ("_seen",)

    def __init__(self, initial: set[str] | None = None) -> None:
        self._seen: set[str] = set(initial or ())

    def add(self, url: str) -> bool:
        """Record ``url``; return True if it is new."""
        if url in self._seen:
            return False
        self._seen.add(url)
        return True

    def __contains__(self, url: str) -> bool:
        return url in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def snapshot(self) -> list[str]:
        """The URLs, for checkpointing."""
        return sorted(self._seen)


class BloomFilter:
    """A probabilistic set: constant memory, no false negatives.

    How it works: keep ``m`` bits, all zero. To add an item, hash it ``k``
    different ways and set those ``k`` bits. To test an item, hash it the same
    ``k`` ways and check whether all those bits are set.

    If any bit is zero the item was definitely never added -- **no false
    negatives**, which is what makes it safe here. If all ``k`` bits are set
    the item is *probably* present, but other items could have set those bits
    between them, so a false positive is possible.

    For a crawler the trade is explicit: a false positive means a page is
    skipped that should have been fetched. At the default 1% error rate, one
    page in a hundred is silently missed, in exchange for roughly 1.2 bytes
    per URL instead of 100. Worth it at a hundred million URLs, not at ten
    thousand -- which is why ``UrlSet`` is the default.

    Sizing follows the standard formulas: ``m = -n ln p / (ln 2)^2`` bits and
    ``k = (m/n) ln 2`` hash functions, for ``n`` expected items at a target
    false-positive rate ``p``.
    """

    __slots__ = ("size", "hashes", "_bits", "_added")

    def __init__(self, expected_items: int = 1_000_000, error_rate: float = 0.01) -> None:
        if expected_items <= 0:
            raise ValueError("expected_items must be positive")
        if not 0.0 < error_rate < 1.0:
            raise ValueError("error_rate must be between 0 and 1")
        self.size = max(8, int(-expected_items * math.log(error_rate) / (math.log(2) ** 2)))
        self.hashes = max(1, round(self.size / expected_items * math.log(2)))
        self._bits = bytearray((self.size + 7) // 8)
        self._added = 0

    def _positions(self, item: str) -> list[int]:
        """``k`` bit positions, from two hashes rather than ``k`` of them.

        Kirsch-Mitzenmacher: ``h_i = h1 + i * h2`` behaves as well as ``k``
        independent hash functions, so one 128-bit digest is split in half and
        reused. Hashing is the hot path, and this cuts it to a single digest
        per item.
        """
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1  # odd, so it strides the space
        return [(h1 + i * h2) % self.size for i in range(self.hashes)]

    def add(self, item: str) -> bool:
        """Record ``item``; return True if it was (probably) new."""
        positions = self._positions(item)
        new = False
        for position in positions:
            index, offset = divmod(position, 8)
            mask = 1 << offset
            if not self._bits[index] & mask:
                new = True
                self._bits[index] |= mask
        if new:
            self._added += 1
        return new

    def __contains__(self, item: str) -> bool:
        return all(
            self._bits[position // 8] & (1 << (position % 8)) for position in self._positions(item)
        )

    def __len__(self) -> int:
        return self._added

    @property
    def memory_bytes(self) -> int:
        return len(self._bits)

    def estimated_error_rate(self) -> float:
        """Current false-positive probability, given how full the filter is."""
        if self._added == 0:
            return 0.0
        exponent = -self.hashes * self._added / self.size
        return float((1 - math.exp(exponent)) ** self.hashes)


# ------------------------------------------------------------------ content


def content_hash(text: str) -> str:
    """A stable hash of a page's text, ignoring whitespace differences.

    Whitespace is collapsed first so that a reformatted template, or the same
    page served with different indentation, hashes the same.
    """
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _shingles(text: str, size: int = 4) -> list[str]:
    """Overlapping word n-grams.

    Shingles rather than single words, because word *frequencies* are too
    similar across pages on one site -- a shared navigation menu would make
    every page look alike. Overlapping runs of four words capture phrasing,
    which is what actually differs between two articles.
    """
    words = _WORD.findall(text.lower())
    if len(words) < size:
        return [" ".join(words)] if words else []
    return [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]


def simhash(text: str, shingle_size: int = 4) -> int:
    """A 64-bit fingerprint where similar documents get similar fingerprints.

    Ordinary hashes are designed so that one changed byte scrambles the whole
    output -- exactly wrong for finding near-duplicates. SimHash is built to
    do the opposite:

    1. break the text into shingles
    2. hash each shingle to 64 bits
    3. for each bit position, add 1 if that shingle's bit is set, subtract 1
       if not
    4. the fingerprint's bit is 1 where the total ended up positive

    Each bit is a vote across every shingle, so changing a few shingles moves
    few votes across zero, and the fingerprint barely changes. Documents are
    then near-duplicates when their fingerprints differ in only a handful of
    bits.
    """
    vector = [0] * 64
    shingles = _shingles(text, shingle_size)
    if not shingles:
        return 0
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += 1 if value >> bit & 1 else -1

    fingerprint = 0
    for bit in range(64):
        if vector[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """How many bits differ between two fingerprints."""
    return (a ^ b).bit_count()


@dataclass
class ContentDeduplicator:
    """Detects exact and near-duplicate page text.

    Near-duplicate lookup is the interesting part. Comparing a new fingerprint
    against every stored one is O(n) per page, which is O(n^2) over a crawl.
    Instead the 64-bit fingerprint is cut into ``bands`` equal pieces and each
    piece is indexed, so only pages sharing a whole band are compared.

    That is not a heuristic: if two fingerprints differ in at most
    ``max_distance`` bits and there are more than ``max_distance`` bands, then
    by the pigeonhole principle at least one band must be identical. So with 4
    bands and a threshold of 3, this finds **every** pair within the threshold
    while comparing almost none of them. (It is the same banding idea behind
    MinHash LSH.)

    Choosing the threshold
    ----------------------
    The literature's default for 64-bit fingerprints is 3, from Manku et al.
    (2007). Measured on this project's own corpora it catches only **51%** of
    real near-duplicates, because that paper's documents are far longer than a
    typical page: the same one-sentence edit is 12 bits away in a 70-word page
    and 1 bit away in an 800-word one.

    So the default here was measured rather than inherited
    (``benchmarks/bench_simhash.py``):

    ====================  ==================  ==================
    threshold             near-dups caught    false positives
    ====================  ==================  ==================
    3 (the literature)    51%                 0%
    **7 (this default)**  **80%**             **0%**
    12                    97%                 0%
    ====================  ==================  ==================

    Unrelated documents never came closer than 25 bits, so there is a wide
    empty gap and no threshold in this range produces false positives. The
    reason not to simply take 12 is the guarantee below: it needs
    ``bands > max_distance``, and since ``bands`` must divide 64, a threshold
    of 12 forces 16 bands of 4 bits each -- keys so short that every bucket
    collides and the index stops narrowing anything. 7 with 8 bands is the
    most sensitive setting that keeps both the guarantee and selective keys.

    Parameters
    ----------
    max_distance:
        Bits that may differ before two pages count as different.
    bands:
        Must be greater than ``max_distance`` for the guarantee to hold, and
        must divide 64.
    """

    max_distance: int = 7
    bands: int = 8
    _exact: dict[str, str] = field(default_factory=dict, repr=False)
    _buckets: dict[tuple[int, int], list[tuple[int, str]]] = field(default_factory=dict, repr=False)
    exact_hits: int = 0
    near_hits: int = 0

    def __post_init__(self) -> None:
        if self.bands <= self.max_distance:
            raise ValueError(
                f"bands ({self.bands}) must exceed max_distance ({self.max_distance}) "
                "or near-duplicates can be missed"
            )
        if 64 % self.bands:
            raise ValueError("bands must divide 64")
        self._band_bits = 64 // self.bands

    def _band_keys(self, fingerprint: int) -> list[tuple[int, int]]:
        mask = (1 << self._band_bits) - 1
        return [
            (index, fingerprint >> (index * self._band_bits) & mask) for index in range(self.bands)
        ]

    def check(self, text: str, url: str) -> str | None:
        """Return the URL this page duplicates, or ``None`` if it is new.

        Registers the page when it is new, so one call per page is enough.
        """
        digest = content_hash(text)
        original = self._exact.get(digest)
        if original is not None:
            self.exact_hits += 1
            return original

        fingerprint = simhash(text)
        if fingerprint:
            seen_pairs: set[int] = set()
            for key in self._band_keys(fingerprint):
                for other, other_url in self._buckets.get(key, ()):
                    if other in seen_pairs:
                        continue
                    seen_pairs.add(other)
                    if hamming_distance(fingerprint, other) <= self.max_distance:
                        self.near_hits += 1
                        return other_url

        self._exact[digest] = url
        if fingerprint:
            for key in self._band_keys(fingerprint):
                self._buckets.setdefault(key, []).append((fingerprint, url))
        return None

    @property
    def unique_pages(self) -> int:
        return len(self._exact)
