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

    # Transcript persisted and referenced so the reviewer can re-check quotes.
    assert result.transcript_path.exists()
    assert ev["transcript_ref"] == "data/transcripts/" + ev["id"] + ".md"

    assert len(result.stance_paths) == 1
    assert result.stance_paths[0].exists()

    # Non-housing captured separately, unpublished.
    assert result.other_path is not None and result.other_path.exists()
    assert (tmp_path / "positions" / "other") in result.other_path.parents

    assert "Example Chicago News" in result.pr_body
    assert result.housing_count == 1


def test_transcript_path_for_is_derived_from_data_dir_not_ref_prefix(tmp_path):
    # Robust regardless of the data dir's name (the review CLI relies on this).
    ev = {"id": "2026-05-29-x", "transcript_ref": "data/transcripts/2026-05-29-x.md"}
    path = run.transcript_path_for(tmp_path, ev)
    assert path == tmp_path / "transcripts" / "2026-05-29-x.md"


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
