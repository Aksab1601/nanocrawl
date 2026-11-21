"""The command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanocrawl.cli import build_parser, config_from_args, main
from nanocrawl.store import read_corpus


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


class TestArguments:
    def test_seeds_are_collected(self) -> None:
        args = parse(["https://a.com", "https://b.com"])
        assert args.seeds == ["https://a.com", "https://b.com"]

    def test_zero_pages_means_unlimited(self) -> None:
        config = config_from_args(parse(["--max-pages", "0"]), ["https://a.com"])
        assert config.max_pages is None

    def test_scope_flags_map_through(self) -> None:
        args = parse(
            [
                "--off-site",
                "--no-subdomains",
                "--allow-prefix",
                "/docs",
                "--allow-prefix",
                "/guide",
                "--deny-prefix",
                "/archive",
            ]
        )
        config = config_from_args(args, ["https://a.com"])
        assert config.same_site_only is False
        assert config.include_subdomains is False
        assert config.allow_prefixes == ("/docs", "/guide")
        assert config.deny_prefixes == ("/archive",)

    def test_politeness_defaults_are_conservative(self) -> None:
        config = config_from_args(parse([]), ["https://a.com"])
        assert config.delay >= 0.5
        assert config.max_pages is not None
        assert config.same_site_only is True

    def test_content_flags_map_through(self) -> None:
        args = parse(["--keep-boilerplate", "--no-dedup", "--min-words", "5", "--bloom"])
        config = config_from_args(args, ["https://a.com"])
        assert config.drop_boilerplate is False
        assert config.near_duplicates is False
        assert config.min_words == 5
        assert config.use_bloom is True

    def test_resume_path_maps_through(self) -> None:
        config = config_from_args(parse(["--resume", "state.json"]), ["https://a.com"])
        assert config.checkpoint_path == Path("state.json")

    def test_there_is_no_flag_to_ignore_robots(self) -> None:
        """A deliberate absence, asserted so nobody adds one by accident."""
        help_text = build_parser().format_help()
        assert "--ignore-robots" not in help_text
        assert "robots.txt is always respected" in help_text


class TestRun:
    def test_demo_crawls_and_writes_a_corpus(self, tmp_path: Path, capsys) -> None:
        output = tmp_path / "corpus.jsonl"
        code = main(["--demo", "--out", str(output), "--delay", "0", "--max-pages", "20"])
        assert code == 0
        records = list(read_corpus(output))
        assert len(records) > 3
        assert all(record["text"] for record in records)

        printed = capsys.readouterr().out
        assert "crawled" in printed
        assert "nanosearch build" in printed  # the bridge is advertised

    def test_stats_json_is_written(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "stats.json"
        main(
            [
                "--demo",
                "--out",
                str(tmp_path / "c.jsonl"),
                "--delay",
                "0",
                "--max-pages",
                "10",
                "--stats-json",
                str(stats_path),
                "--quiet",
            ]
        )
        payload = json.loads(stats_path.read_text())
        assert payload["pages_fetched"] > 0
        assert "status_codes" in payload

    def test_quiet_prints_nothing(self, tmp_path: Path, capsys) -> None:
        main(
            [
                "--demo",
                "--out",
                str(tmp_path / "c.jsonl"),
                "--delay",
                "0",
                "--max-pages",
                "5",
                "--quiet",
            ]
        )
        # The demo banner is the one line that always prints.
        assert "crawled" not in capsys.readouterr().out

    def test_no_seeds_is_an_error(self, capsys) -> None:
        assert main([]) == 2
        assert "seed URL" in capsys.readouterr().err

    def test_unreachable_seed_exits_nonzero(self, tmp_path: Path) -> None:
        code = main(
            [
                "http://127.0.0.1:1/",
                "--out",
                str(tmp_path / "c.jsonl"),
                "--timeout",
                "0.5",
                "--max-pages",
                "2",
                "--quiet",
            ]
        )
        assert code == 1

    def test_invalid_arguments_exit(self) -> None:
        with pytest.raises(SystemExit):
            main(["--max-pages", "not-a-number"])
