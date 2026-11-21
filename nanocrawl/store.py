"""Writing the corpus.

Output is JSON Lines: one JSON object per page, one page per line. It is the
right format here for three reasons -- it can be appended to while a crawl is
running, it can be read back a line at a time without loading the file, and a
crash leaves every completed line intact rather than a truncated array.

The schema is deliberately the one ``nanosearch`` reads, so a crawl feeds
straight into an index:

    nanocrawl https://example.com --out corpus.jsonl
    nanosearch build corpus.jsonl --out ./index
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["Document", "JsonlWriter", "read_corpus"]


@dataclass
class Document:
    """One crawled page, as it is stored.

    ``text`` is first so that a reader skimming the file sees the content
    rather than the metadata, and because ``nanosearch`` reads the ``text``
    field and treats everything else as metadata.
    """

    text: str
    url: str
    title: str = ""
    depth: int = 0
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    status: int | None = None
    content_hash: str = ""
    word_count: int = 0
    language: str | None = None
    referrer: str | None = None

    def to_json(self) -> str:
        # ensure_ascii=False keeps non-English text readable in the file
        # rather than escaping it into \uXXXX noise.
        return json.dumps(asdict(self), ensure_ascii=False)


class JsonlWriter:
    """Appends documents to a JSONL file, flushing as it goes.

    Flushing each line costs a little throughput and buys the ability to stop
    a long crawl with Ctrl-C and still have every page it had written. For a
    job that may run for hours that is the right trade.
    """

    def __init__(self, path: str | Path, append: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a" if append else "w", encoding="utf-8")
        self.written = 0

    def write(self, document: Document) -> None:
        self._handle.write(document.to_json() + "\n")
        self._handle.flush()
        self.written += 1

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_corpus(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream a JSONL corpus back, one record at a time."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
