"""Shared fixtures: a real HTTP server, on a real socket, on localhost.

The crawler is tested against an actual server rather than a mocked transport.
A mock would pass while the real thing failed on redirects, chunked encoding,
connection reuse or content-length headers -- which is most of what a crawler
has to get right.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from nanocrawl.testing import SiteServer, build_site


@pytest.fixture
def site() -> Iterator[SiteServer]:
    """A fresh fake site per test, so request counts start at zero."""
    server = SiteServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def pages() -> dict:
    return build_site()
