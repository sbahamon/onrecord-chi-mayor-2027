"""Backfill orchestration: a bounded list of URLs -> one bucket per candidate.

Reuses the tested units (ingest -> extract -> propose via run.process_source),
so this runs offline with an injected LLM + fetcher. It proves the middle mode
between one-URL intake and the daily discover run: group output by candidate
(one PR each), keep each candidate's page attributable only to them, and mark
every processed URL in the ledger.
"""
import json
from pathlib import Path

from pipeline import backfill, discover

ARTICLE_HTML = (Path(__file__).parent / "fixtures" / "article.html").read_text()

# Verbatim fragments present in ARTICLE_HTML (quote-in-transcript must hold).
HOUSING_QUOTE = (
    "We can't say we want affordability and then ban apartments in half the\n    city,"
)
OTHER_QUOTE = "driver of the city's housing shortage."


def housing_stmt(candidate, topic="zoning-reform"):
    return {
        "candidate": candidate, "topic": topic, "stance": "supports",
        "summary": "Would legalize apartment buildings in every neighborhood.",
        "quote": HOUSING_QUOTE, "locator": None, "confidence": 0.9,
        "is_housing": True, "attribution_flag": False,
    }


class FakeLLM:
    """Returns canned statements keyed by the candidate slug in the user prompt.

    ``extract.build_user_prompt`` puts ``Candidates (slugs): <slugs>`` on the
    first line. Keying off it lets one fake serve many candidates AND proves
    that backfill scopes each row to exactly one candidate: if it passed the
    whole roster, the parsed key would be a comma list and match nothing.
    """

    def __init__(self, by_candidate):
        self.by_candidate = by_candidate
        self.seen_candidate_lines = []

    def complete_json(self, *, model, system, user):
        first = user.splitlines()[0]
        slug = first.split(":", 1)[1].strip()
        self.seen_candidate_lines.append(slug)
        return {"statements": self.by_candidate.get(slug, [])}


def row(candidate_slug, url, outlet):
    return {"candidate_slug": candidate_slug, "url": url, "type": "website",
            "outlet": outlet}


def test_groups_output_into_one_bucket_per_candidate(tmp_path):
    rows = [
        row("cand-a", "https://a.example/issues", "A for Mayor"),
        row("cand-b", "https://b.example/issues", "B for Mayor"),
    ]
    llm = FakeLLM({
        "cand-a": [housing_stmt("cand-a")],
        "cand-b": [housing_stmt("cand-b")],
    })

    buckets = backfill.run_backfill(
        rows, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    by_slug = {b.candidate_slug: b for b in buckets}
    assert set(by_slug) == {"cand-a", "cand-b"}

    # Each row was extracted scoped to exactly its own candidate.
    assert llm.seen_candidate_lines == ["cand-a", "cand-b"]

    for slug, outlet in [("cand-a", "A for Mayor"), ("cand-b", "B for Mayor")]:
        b = by_slug[slug]
        assert b.housing_count == 1
        assert outlet in b.pr_body
        # stance written under this candidate's own directory
        assert (tmp_path / "stances" / slug / "zoning-reform.json").exists()
        assert any(
            (tmp_path / "stances" / slug) in Path(p).parents for p in b.paths
        )


def test_multiple_rows_for_same_candidate_collapse_into_one_bucket(tmp_path):
    rows = [
        row("cand-a", "https://a.example/issues", "A for Mayor"),
        row("cand-a", "https://a.example/platform", "A for Mayor"),
    ]
    llm = FakeLLM({"cand-a": [housing_stmt("cand-a")]})

    buckets = backfill.run_backfill(
        rows, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    assert len(buckets) == 1
    b = buckets[0]
    assert b.candidate_slug == "cand-a"
    assert len(b.results) == 2
    assert b.housing_count == 2
    # combined body joins the two hits with a divider
    assert "---" in b.pr_body


def test_statement_misattributed_to_another_candidate_is_dropped(tmp_path):
    # The model tags cand-a's own page with a statement about cand-b.
    rows = [row("cand-a", "https://a.example/issues", "A for Mayor")]
    llm = FakeLLM({"cand-a": [housing_stmt("cand-b")]})  # wrong candidate

    buckets = backfill.run_backfill(
        rows, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    assert len(buckets) == 1
    assert buckets[0].housing_count == 0
    # cross-attribution never reaches another candidate's matrix cell
    assert not (tmp_path / "stances" / "cand-b").exists()


def test_marks_every_processed_url_in_the_ledger(tmp_path):
    rows = [
        row("cand-a", "https://a.example/issues", "A for Mayor"),
        row("cand-b", "https://b.example/issues", "B for Mayor"),
    ]
    llm = FakeLLM({
        "cand-a": [housing_stmt("cand-a")],
        "cand-b": [housing_stmt("cand-b")],
    })
    ledger = discover.Ledger(tmp_path / "ledger.json")

    backfill.run_backfill(
        rows, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"], ledger=ledger,
        fetcher=lambda url: ARTICLE_HTML,
    )

    reloaded = discover.Ledger(tmp_path / "ledger.json")
    assert not reloaded.is_new("https://a.example/issues")
    assert not reloaded.is_new("https://b.example/issues")


class FlakyLLM:
    """Fails (returns an out-of-range statement) N times, then returns good output.

    Models are nondeterministic; deepseek occasionally emits one malformed
    statement (confidence -1, empty quote) that ``extract`` rightly rejects by
    raising. For a one-time backfill of a page we know has content, retrying the
    row usually yields a clean batch — that resilience is what these tests pin.
    """

    def __init__(self, fail_times, good):
        self.remaining_fails = fail_times
        self.good = good
        self.calls = 0

    def complete_json(self, *, model, system, user):
        self.calls += 1
        if self.remaining_fails > 0:
            self.remaining_fails -= 1
            bad = dict(self.good, confidence=-1)  # schema minimum is 0 -> extract raises
            return {"statements": [bad]}
        return {"statements": [self.good]}


def test_retries_a_transient_extraction_failure(tmp_path):
    llm = FlakyLLM(fail_times=1, good=housing_stmt("cand-a"))
    buckets = backfill.run_backfill(
        [row("cand-a", "https://a.example/issues", "A for Mayor")],
        data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"], max_attempts=3,
        fetcher=lambda url: ARTICLE_HTML,
    )
    assert llm.calls == 2  # failed once, then succeeded
    assert buckets[0].housing_count == 1
    assert buckets[0].errors == []


def test_a_persistently_failing_row_is_recorded_not_raised(tmp_path):
    llm = FlakyLLM(fail_times=99, good=housing_stmt("cand-a"))  # never recovers
    ledger = discover.Ledger(tmp_path / "ledger.json")
    buckets = backfill.run_backfill(
        [row("cand-a", "https://a.example/issues", "A for Mayor")],
        data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"], ledger=ledger,
        max_attempts=3, fetcher=lambda url: ARTICLE_HTML,
    )
    assert llm.calls == 3  # tried max_attempts, gave up
    assert buckets[0].housing_count == 0
    assert len(buckets[0].errors) == 1
    # a failed URL is NOT marked seen, so it can be re-run later
    assert discover.Ledger(tmp_path / "ledger.json").is_new("https://a.example/issues")


def test_without_a_ledger_no_ledger_file_is_written(tmp_path):
    rows = [row("cand-a", "https://a.example/issues", "A for Mayor")]
    llm = FakeLLM({"cand-a": [housing_stmt("cand-a")]})

    backfill.run_backfill(
        rows, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", topics=["zoning-reform"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    assert not (tmp_path / "ledger.json").exists()
