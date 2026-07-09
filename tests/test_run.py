"""Integration of the tested units: source -> ingest -> extract -> files + PR body.

Everything is injected (fetcher + llm), so this runs offline. It proves the glue
writes the right files to the right places and separates housing from the rest.
"""
import json
from pathlib import Path

from pipeline import run

ARTICLE_HTML = (Path(__file__).parent / "fixtures" / "article.html").read_text()

SOURCE = {
    "url": "https://news.example.com/doe-apartments",
    "outlet": "Example Chicago News",
    "media_type": "article",
    "title": "Doe pitches citywide apartment legalization",
    "published_date": "2026-07-06",
}


class FakeLLM:
    def __init__(self, statements):
        self.statements = statements

    def complete_json(self, *, model, system, user):
        return {"statements": self.statements}


def housing_and_other_llm():
    return FakeLLM([
        {
            "candidate": "example-candidate-a", "topic": "zoning-reform",
            "stance": "supports",
            "summary": "Would legalize apartment buildings in every neighborhood.",
            "quote": "We can't say we want affordability and then ban apartments in half the\n    city,",
            "locator": None, "confidence": 0.9, "is_housing": True,
            "attribution_flag": False,
        },
        {
            "candidate": "example-candidate-a", "topic": "schools",
            "stance": "supports", "summary": "Off-topic capture.",
            "quote": "driver of the city's housing shortage.",
            "locator": None, "confidence": 0.6, "is_housing": False,
            "attribution_flag": False,
        },
    ])


class FlakyLLM:
    """Returns a structurally-broken payload (no 'statements' key) on the first
    call, then a valid one — mimicking a transient malformed-JSON response that
    extract raises on and process_source retries.
    """
    def __init__(self, payloads):
        self._payloads = payloads
        self.calls = 0

    def complete_json(self, *, model, system, user):
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return self._payloads[idx]


def test_process_source_retries_extract_on_structural_failure(tmp_path):
    # extract.py raises on a structurally-broken response (missing 'statements');
    # process_source should retry the extraction (reusing the transcript, so no
    # re-download/re-transcribe) rather than aborting the whole source. (A lone
    # malformed *statement* is dropped, not retried — see test_extract.py.)
    good_stmt = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports",
        "summary": "Would legalize apartment buildings in every neighborhood.",
        "quote": "We can't say we want affordability and then ban apartments in half the\n    city,",
        "locator": None, "confidence": 0.9, "is_housing": True,
        "attribution_flag": False,
    }

    llm = FlakyLLM([{"oops": "malformed"}, {"statements": [good_stmt]}])
    result = run.process_source(
        SOURCE,
        data_dir=tmp_path,
        llm=llm,
        extractor_model="fake",
        today="2026-07-07",
        candidates=["example-candidate-a"],
        topics=["zoning-reform"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    assert llm.calls == 2  # retried once after the transient structural failure
    assert result.evidence_path.exists()
    assert result.housing_count == 1


def test_process_source_writes_evidence_stance_and_other(tmp_path):
    result = run.process_source(
        SOURCE,
        data_dir=tmp_path,
        llm=housing_and_other_llm(),
        extractor_model="fake",
        today="2026-07-07",
        candidates=["example-candidate-a"],
        topics=["zoning-reform", "schools"],
        fetcher=lambda url: ARTICLE_HTML,
    )

    assert result.evidence_path.exists()
    ev = json.loads(result.evidence_path.read_text())
    assert len(ev["statements"]) == 1  # only the housing statement

    # Transcripts are NOT stored (copyright); the reviewer re-ingests instead.
    assert result.transcript_path is None
    assert ev["transcript_ref"] is None
    assert not (tmp_path / "transcripts").exists()

    assert len(result.stance_paths) == 1
    assert result.stance_paths[0].exists()

    # Non-housing captured separately, unpublished.
    assert result.other_path is not None and result.other_path.exists()
    assert (tmp_path / "positions" / "other") in result.other_path.parents

    assert "Example Chicago News" in result.pr_body
    assert result.housing_count == 1


def test_process_source_with_no_housing_writes_no_evidence(tmp_path):
    llm = FakeLLM([{
        "candidate": "example-candidate-a", "topic": "schools",
        "stance": "supports", "summary": "Schools only.",
        "quote": "driver of the city's housing shortage.",
        "locator": None, "confidence": 0.6, "is_housing": False,
        "attribution_flag": False,
    }])
    result = run.process_source(
        SOURCE, data_dir=tmp_path, llm=llm, extractor_model="fake",
        today="2026-07-07", candidates=["example-candidate-a"],
        topics=["schools"], fetcher=lambda url: ARTICLE_HTML,
    )
    assert result.evidence_path is None
    assert result.stance_paths == []
    assert result.housing_count == 0


def test_write_other_refuses_path_traversal(tmp_path):
    # Consistency with write_stance/write_evidence: the other-capture writer must
    # also refuse an id that escapes data/positions/other/.
    import pytest

    doc = {
        "id": "../../ledger", "url": "https://x.example/a", "outlet": "X",
        "published_date": "2026-07-07",
    }
    with pytest.raises(ValueError):
        run._write_other(doc, [], tmp_path)
    assert not (tmp_path / "ledger.json").exists()
