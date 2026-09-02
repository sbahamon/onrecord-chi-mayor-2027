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


# --- #101: papercuts found running #56 ---

def test_ingest_url_accepts_a_locally_saved_page():
    """Some outlets 403 every IP available — Crain's returned 403 to a residential
    IP and to Anthropic's infrastructure alike. #97 needed a bespoke inline script
    injecting a fetcher lambda, twice."""
    args = build_parser().parse_args(
        ["ingest-url", "--url", "https://example.com/a", "--html-file", "/tmp/saved.html"]
    )
    assert args.html_file == "/tmp/saved.html"


def test_backfill_does_not_seed_the_ledger_by_default():
    """CLAUDE.md and backfill.yml both say backfill seeds no ledger entries (#61),
    but the CLI marked it unless --skip-ledger was passed. #97 wrote 7 URLs that had
    to be reverted by hand. Make the code agree with the convention."""
    args = build_parser().parse_args(["backfill", "--input", "rows.json"])
    assert args.seed_ledger is False


def test_backfill_can_still_opt_in_to_seeding_the_ledger():
    args = build_parser().parse_args(["backfill", "--input", "rows.json", "--seed-ledger"])
    assert args.seed_ledger is True
