"""HTML parsing: links out, readable text out, malformed markup survived."""

from __future__ import annotations

from nanocrawl.parse import parse_html

PAGE = """<!DOCTYPE html>
<html lang="en-GB">
<head>
  <title>  The   Page Title </title>
  <base href="https://example.com/base/">
  <meta name="description" content="A short summary.">
  <script>var tracking = {a: 1};</script>
  <style>body { color: red }</style>
</head>
<body>
  <nav>Home About Contact</nav>
  <header>Site name</header>
  <main>
    <h1>Real heading</h1>
    <p>The first paragraph of the actual article content goes here and runs on
       for a while so that the extractor has something substantial to find.</p>
    <p>A second paragraph, also part of the content.</p>
  </main>
  <aside>Related links sidebar</aside>
  <footer>Copyright 2026</footer>
  <a href="/one">one</a>
  <a href="two.html">two</a>
  <a href="https://other.com/three">three</a>
  <a href="/sponsored" rel="nofollow">sponsored</a>
  <a>no href at all</a>
</body></html>"""


class TestText:
    def test_title_is_collapsed(self) -> None:
        assert parse_html(PAGE).title == "The Page Title"

    def test_script_and_style_contents_are_dropped(self) -> None:
        text = parse_html(PAGE).text
        assert "tracking" not in text
        assert "color: red" not in text

    def test_boilerplate_is_dropped(self) -> None:
        text = parse_html(PAGE).text
        for noise in ("Home About Contact", "Site name", "Copyright 2026"):
            assert noise not in text

    def test_main_content_survives(self) -> None:
        text = parse_html(PAGE).text
        assert "Real heading" in text
        assert "first paragraph" in text
        assert "second paragraph" in text

    def test_boilerplate_can_be_kept(self) -> None:
        assert "Home About Contact" in parse_html(PAGE, drop_boilerplate=False).text

    def test_block_tags_become_line_breaks(self) -> None:
        page = parse_html("<p>one</p><p>two</p>")
        assert page.text == "one\ntwo"

    def test_whitespace_is_collapsed(self) -> None:
        assert parse_html("<p>a     b\n\n\n   c</p>").text == "a b c"

    def test_entities_are_decoded(self) -> None:
        assert "Tom & Jerry" in parse_html("<p>Tom &amp; Jerry</p>").text

    def test_word_count(self) -> None:
        assert parse_html("<p>one two three</p>").word_count == 3


class TestLinks:
    def test_collects_hrefs_in_document_order(self) -> None:
        links = parse_html(PAGE).links
        assert links == ["/one", "two.html", "https://other.com/three"]

    def test_nofollow_links_are_skipped(self) -> None:
        assert "/sponsored" not in parse_html(PAGE).links

    def test_anchors_without_href_are_skipped(self) -> None:
        assert len(parse_html(PAGE).links) == 3

    def test_base_href_is_reported(self) -> None:
        assert parse_html(PAGE).base_href == "https://example.com/base/"


class TestMetadata:
    def test_language(self) -> None:
        assert parse_html(PAGE).language == "en-GB"

    def test_description(self) -> None:
        assert parse_html(PAGE).description == "A short summary."

    def test_meta_robots_noindex(self) -> None:
        page = parse_html('<meta name="robots" content="noindex"><p>x</p>')
        assert page.noindex and not page.nofollow

    def test_meta_robots_nofollow(self) -> None:
        page = parse_html('<meta name="robots" content="NOFOLLOW"><p>x</p>')
        assert page.nofollow and not page.noindex

    def test_meta_robots_none_means_both(self) -> None:
        page = parse_html('<meta name="robots" content="none"><p>x</p>')
        assert page.noindex and page.nofollow


class TestRobustness:
    def test_malformed_markup_does_not_raise(self) -> None:
        for markup in (
            "<p>unclosed",
            "</div></div></div>",
            "<a href=>empty</a>",
            "<<>><p>weird</p>",
            "",
            "not html at all",
            "<p>text<",
        ):
            parse_html(markup)  # must not raise

    def test_main_is_ignored_when_nearly_empty(self) -> None:
        # Some sites wrap a sidebar in <main> and put the article beside it;
        # trusting <main> blindly would throw the page away.
        markup = (
            "<main>tiny</main><p>"
            + "The real content of this page is out here and is long enough to matter. " * 3
            + "</p>"
        )
        assert "real content" in parse_html(markup).text

    def test_deeply_nested_markup(self) -> None:
        markup = "<div>" * 200 + "<p>deep</p>" + "</div>" * 200
        assert "deep" in parse_html(markup).text
