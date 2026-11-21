"""URL normalisation and filtering: deciding what counts as "the same page".

This is the least glamorous module and the one that decides whether a crawl
terminates. The web is full of URLs that point at the same page:

    http://Example.com/a/           https://example.com/a
    https://example.com/a?b=1&c=2   https://example.com/a?c=2&b=1
    https://example.com/a#section   https://example.com/a

A crawler that treats those as four different pages will fetch the same
content four times, and on a site with a calendar widget it will never stop at
all. Normalisation collapses them to one key, and that key is what the visited
set stores.

Normalisation here is deliberately *conservative*: every rule below is one the
HTTP and URI standards say is safe. Stripping a trailing slash from a path, by
contrast, is not safe in general -- some servers genuinely serve different
content at ``/a`` and ``/a/`` -- so it is offered as an option and left off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

__all__ = ["UrlFilter", "normalise", "registrable_host", "same_site"]

DEFAULT_PORTS = {"http": 80, "https": 443}

# Characters RFC 3986 says never need escaping, so %7E and ~ are the same URL.
_UNRESERVED = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _decode_unreserved(text: str) -> str:
    """Decode percent-escapes that never needed escaping, uppercase the rest.

    ``/~alex`` and ``/%7Ealex`` are the same resource, and so are ``%7e`` and
    ``%7E``. Normalising both is RFC 3986 section 6.2.2.
    """

    def replace(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED_CHARS else "%" + match.group(1).upper()

    return _UNRESERVED.sub(replace, text)


def _remove_dot_segments(path: str) -> str:
    """Resolve ``.`` and ``..`` inside a path, per RFC 3986 section 5.2.4.

    ``/a/b/../c`` is the same resource as ``/a/c``, and a crawler that does
    not know this will fetch both. Leading ``..`` segments are dropped rather
    than escaping above the root, which is what the standard requires.
    """
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] != "":
                output.pop()
            continue
        output.append(segment)
    resolved = "/".join(output)
    # A path ending in "." or ".." leaves a trailing slash behind.
    if path.endswith(("/.", "/..")) and not resolved.endswith("/"):
        resolved += "/"
    return resolved


# Extensions that are almost never useful text and expensive to download.
BINARY_SUFFIXES = frozenset(
    [
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".bmp",
        ".tiff",
        ".mp4",
        ".webm",
        ".avi",
        ".mov",
        ".mkv",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".iso",
        ".dmg",
        ".exe",
        ".msi",
        ".apk",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".css",
        ".js",
        ".map",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
    ]
)


def normalise(url: str, base: str | None = None, strip_trailing_slash: bool = False) -> str:
    """Return a canonical form of ``url``, resolved against ``base`` if given.

    Applied in order:

    1. resolve relative references against ``base`` (``../x`` -> absolute)
    2. drop the fragment -- ``#section`` is a position *inside* a page, never a
       different page, and is never sent to the server
    3. lowercase the scheme and host (both are case-insensitive by RFC 3986;
       the path is not and must be left alone)
    4. drop the default port, so ``https://x:443/`` equals ``https://x/``
    5. resolve ``.`` and ``..`` segments in the path
    6. decode percent-escapes that were never needed, and uppercase the rest
    7. sort query parameters, so argument order stops creating new URLs
    8. give an empty path an explicit ``/``
    """
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    parts = urlsplit(url)

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""

    netloc = host
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = _decode_unreserved(_remove_dot_segments(parts.path or "/")) or "/"
    if strip_trailing_slash and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # keep_blank_values: ``?debug`` and ``?debug=`` are not the same to every
    # server, and dropping a blank value would merge two distinct URLs.
    query = urlencode(sorted(parse_qsl(_decode_unreserved(parts.query), keep_blank_values=True)))

    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_host(url: str) -> str:
    """The host, with a leading ``www.`` removed.

    Treating ``example.com`` and ``www.example.com`` as one site is what people
    mean by "stay on this site". A full public-suffix list would be needed to
    do this properly for things like ``foo.github.io``; this is the honest
    90% version.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, other: str, include_subdomains: bool = True) -> bool:
    """Whether two URLs belong to the same site."""
    a, b = registrable_host(url), registrable_host(other)
    if not a or not b:
        return False
    if a == b:
        return True
    if not include_subdomains:
        return False
    return a.endswith("." + b) or b.endswith("." + a)


@dataclass
class UrlFilter:
    """Decides which discovered links are worth queueing.

    Parameters
    ----------
    seeds:
        The URLs the crawl started from. Used for the same-site check.
    same_site_only:
        Stay within the seed sites. On by default, and the single most
        important safety rail: without it, one link to Wikipedia turns a
        documentation crawl into a crawl of the entire web.
    include_subdomains:
        Whether ``docs.example.com`` counts as the same site as
        ``example.com``.
    allow_prefixes / deny_prefixes:
        Path prefixes to restrict to, or to avoid. ``deny`` wins over
        ``allow``.
    allowed_schemes:
        Only ``http`` and ``https`` are fetchable. ``mailto:``, ``javascript:``
        and ``tel:`` links appear in real pages constantly.
    """

    seeds: tuple[str, ...] = ()
    same_site_only: bool = True
    include_subdomains: bool = True
    allow_prefixes: tuple[str, ...] = ()
    deny_prefixes: tuple[str, ...] = ()
    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    skip_binary: bool = True

    def allows(self, url: str) -> bool:
        """Whether ``url`` should be queued."""
        parts = urlsplit(url)
        if parts.scheme not in self.allowed_schemes:
            return False
        if not parts.hostname:
            return False

        path = parts.path or "/"
        if self.skip_binary:
            lowered = path.lower()
            if any(lowered.endswith(suffix) for suffix in BINARY_SUFFIXES):
                return False

        if any(path.startswith(prefix) for prefix in self.deny_prefixes):
            return False
        if self.allow_prefixes and not any(path.startswith(p) for p in self.allow_prefixes):
            return False

        if self.same_site_only and self.seeds:
            return any(same_site(url, seed, self.include_subdomains) for seed in self.seeds)
        return True

    def reason(self, url: str) -> str:
        """Why a URL was rejected -- for the stats report, so skips are visible.

        A crawler that silently drops 90% of what it finds is impossible to
        debug. Counting rejections by reason turns "it only fetched 12 pages"
        into "it rejected 4,000 off-site links".
        """
        parts = urlsplit(url)
        if parts.scheme not in self.allowed_schemes:
            return "scheme"
        if not parts.hostname:
            return "no-host"
        path = parts.path or "/"
        if self.skip_binary and any(path.lower().endswith(s) for s in BINARY_SUFFIXES):
            return "binary"
        if any(path.startswith(p) for p in self.deny_prefixes):
            return "denied-prefix"
        if self.allow_prefixes and not any(path.startswith(p) for p in self.allow_prefixes):
            return "outside-allowed-prefix"
        if (
            self.same_site_only
            and self.seeds
            and not any(same_site(url, seed, self.include_subdomains) for seed in self.seeds)
        ):
            return "off-site"
        return "allowed"
