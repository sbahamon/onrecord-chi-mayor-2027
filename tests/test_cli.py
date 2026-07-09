"""CLI argument wiring (no network — we only parse, never dispatch)."""
import pytest

from pipeline.__main__ import build_parser


def test_ingest_url_requires_url():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest-url"])  # missing --url


def test_ingest_url_parses_options():
    args = build_parser().parse_args(
        ["ingest-url", "--url", "https://x/y", "--type", "podcast", "--title", "T"]
    )
    assert args.cmd == "ingest-url"
    assert args.url == "https://x/y"
    assert args.type == "podcast"
    assert callable(args.func)


def test_review_takes_multiple_evidence_paths():
    args = build_parser().parse_args(["review", "a.json", "b.json"])
    assert args.evidence == ["a.json", "b.json"]


def test_discover_has_default_output():
    args = build_parser().parse_args(["discover"])
    assert args.pr_body_out.endswith(".md")


def test_backfill_requires_input():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["backfill"])  # missing --input


def test_backfill_parses_options():
    args = build_parser().parse_args(
        ["backfill", "--input", "rows.json", "--only", "cand-a",
         "--out-dir", "bodies", "--skip-ledger"]
    )
    assert args.cmd == "backfill"
    assert args.input == "rows.json"
    assert args.only == "cand-a"
    assert args.out_dir == "bodies"
    assert args.skip_ledger is True
    assert callable(args.func)
