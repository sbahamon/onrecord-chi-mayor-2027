"""Assemble extraction results into the files + PR body a review sees.

* evidence record (media hit) — housing statements, schema-valid
* proposed stance cells — one per (candidate, topic), citing the best statement
* PR body — human-readable, with quotes and source links so review is fast
"""
import json

import pytest

from pipeline import propose, schemas

INGEST_DOC = {
    "id": "2026-07-06-example-podcast-doe",
    "url": "https://example.com/ep1",
    "outlet": "Example Podcast",
    "media_type": "podcast",
    "title": "Doe on housing",
    "published_date": "2026-07-06",
    "transcript": "…",
}

HOUSING = [
    {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Backs citywide apartment legalization.",
        "quote": "Legalize apartments everywhere.", "locator": "10:00",
        "confidence": 0.7, "is_housing": True, "attribution_flag": False,
    },
    {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Same topic, higher-confidence take.",
        "quote": "We must end apartment bans, full stop.", "locator": "12:00",
        "confidence": 0.95, "is_housing": True, "attribution_flag": False,
    },
]


def test_build_evidence_record_is_schema_valid_and_has_statements():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    schemas.validate(ev, "evidence")  # must not raise
    assert ev["id"] == INGEST_DOC["id"]
    assert ev["discovered_date"] == "2026-07-07"
    assert len(ev["statements"]) == 2


def test_stance_proposals_pick_highest_confidence_per_candidate_topic():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    stances = propose.propose_stance_updates(ev, today="2026-07-07")
    assert len(stances) == 1  # one (candidate, topic) pair
    s = stances[0]
    schemas.validate(s, "stance")
    # Cites the higher-confidence statement (index 1), not index 0.
    assert s["citations"] == ["2026-07-06-example-podcast-doe#1"]
    assert s["summary"] == "Same topic, higher-confidence take."


def test_pr_body_includes_quotes_candidates_and_source_link():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    stances = propose.propose_stance_updates(ev, today="2026-07-07")
    body = propose.render_pr_body(ev, stances)
    assert "Example Podcast" in body
    assert "https://example.com/ep1" in body
    assert "end apartment bans" in body  # the winning quote
    assert "example-candidate-a" in body


def test_evidence_and_stance_write_to_expected_paths(tmp_path):
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    ev_path = propose.write_evidence(ev, tmp_path)
    assert ev_path == tmp_path / "media-hits" / "2026-07" / f"{ev['id']}.json"
    assert json.loads(ev_path.read_text())["id"] == ev["id"]

    stance = propose.propose_stance_updates(ev, today="2026-07-07")[0]
    s_path = propose.write_stance(stance, tmp_path)
    assert s_path == tmp_path / "stances" / "example-candidate-a" / "zoning-reform.json"


def test_build_evidence_record_rejects_non_housing_statement():
    bad = [dict(HOUSING[0], is_housing=False)]
    with pytest.raises(ValueError):
        propose.build_evidence_record(INGEST_DOC, bad, discovered_date="2026-07-07")
