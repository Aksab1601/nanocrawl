"""URL normalisation: the module that decides whether a crawl terminates."""

from __future__ import annotations

import pytest

from nanocrawl.urls import UrlFilter, normalise, registrable_host, same_site


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # case: scheme and host are case-insensitive, the path is not
            ("HTTP://Example.COM/Path", "http://example.com/Path"),
            # default ports carry no information
            ("http://example.com:80/a", "http://example.com/a"),
            ("https://example.com:443/a", "https://example.com/a"),
            # a non-default port does
            ("https://example.com:8443/a", "https://example.com:8443/a"),
            # fragments are never sent to the server
            ("https://example.com/a#section", "https://example.com/a"),
            # query order is not meaningful
            ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
            # dot segments resolve
            ("https://example.com/a/../b", "https://example.com/b"),
            ("https://example.com/a/./b", "https://example.com/a/b"),
            # an empty path is the root
            ("https://example.com", "https://example.com/"),
            # unreserved characters never needed escaping
            ("https://example.com/%7Ealex", "https://example.com/~alex"),
            # reserved ones still do: %2F is not a path separator
            ("https://example.com/a%2Fb", "https://example.com/a%2Fb"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        assert normalise(raw) == expected

    def test_resolves_relative_references(self) -> None:
        base = "https://example.com/one/two/three"
        assert normalise("../up", base) == "https://example.com/one/up"
        assert normalise("/root", base) == "https://example.com/root"
        assert normalise("sibling", base) == "https://example.com/one/two/sibling"

    def test_blank_query_values_are_preserved(self) -> None:
        # ?debug and ?debug= are not the same to every server, and merging
        # them would silently collapse two distinct URLs into one.
        assert normalise("https://example.com/a?debug=") == "https://example.com/a?debug="

    def test_trailing_slash_is_left_alone_by_default(self) -> None:
        # Servers are allowed to serve different content at /a and /a/, so
        # stripping is opt-in.
        assert normalise("https://example.com/a/") == "https://example.com/a/"
        assert normalise("https://example.com/a/", strip_trailing_slash=True) == (
            "https://example.com/a"
        )

    def test_schemes_without_a_host_normalise_to_empty(self) -> None:
        for raw in ("mailto:a@b.com", "javascript:void(0)", "tel:+1234", "#anchor"):
            assert normalise(raw) == ""

    def test_the_same_page_by_four_routes_collapses_to_one_key(self) -> None:
        """The property the visited-set depends on."""
        variants = [
            "http://Example.com:80/a/b?y=2&x=1#top",
            "http://example.com/a/b?x=1&y=2",
            "http://example.com/a/c/../b?y=2&x=1",
            "http://example.com/./a/b?x=1&y=2#other",
        ]
        assert len({normalise(v) for v in variants}) == 1


class TestSameSite:
    def test_www_is_the_same_site(self) -> None:
        assert same_site("https://www.example.com/a", "https://example.com/b")

    def test_subdomains_are_the_same_site_by_default(self) -> None:
        assert same_site("https://docs.example.com/a", "https://example.com/b")
        assert not same_site(
            "https://docs.example.com/a", "https://example.com/b", include_subdomains=False
        )

    def test_different_domains_are_not(self) -> None:
        assert not same_site("https://example.com/a", "https://example.org/a")
        assert not same_site("https://notexample.com/a", "https://example.com/a")

    def test_registrable_host_strips_www(self) -> None:
        assert registrable_host("https://WWW.Example.com/x") == "example.com"
        assert registrable_host("not a url") == ""


class TestUrlFilter:
    def test_stays_on_site_by_default(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",))
        assert f.allows("https://example.com/page")
        assert not f.allows("https://elsewhere.com/page")
        assert f.reason("https://elsewhere.com/page") == "off-site"

    def test_rejects_unfetchable_schemes(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",))
        assert not f.allows("ftp://example.com/file")
        assert f.reason("ftp://example.com/file") == "scheme"

    def test_rejects_binary_extensions(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",))
        for path in ("/a.pdf", "/b.JPG", "/c.zip", "/d.mp4", "/e.css"):
            assert not f.allows(f"https://example.com{path}"), path
        assert f.reason("https://example.com/a.pdf") == "binary"

    def test_allow_prefix_restricts(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",), allow_prefixes=("/docs",))
        assert f.allows("https://example.com/docs/intro")
        assert not f.allows("https://example.com/blog/post")
        assert f.reason("https://example.com/blog/post") == "outside-allowed-prefix"

    def test_deny_prefix_beats_allow_prefix(self) -> None:
        f = UrlFilter(
            seeds=("https://example.com/",),
            allow_prefixes=("/docs",),
            deny_prefixes=("/docs/archive",),
        )
        assert f.allows("https://example.com/docs/current")
        assert not f.allows("https://example.com/docs/archive/old")

    def test_off_site_allowed_when_asked(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",), same_site_only=False)
        assert f.allows("https://elsewhere.com/page")

    def test_reason_is_allowed_for_accepted_urls(self) -> None:
        f = UrlFilter(seeds=("https://example.com/",))
        assert f.reason("https://example.com/fine") == "allowed"
