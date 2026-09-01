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


def test_write_stance_refuses_path_traversal(tmp_path):
    # Last-line defense: even if a bad topic/candidate reaches write_stance, it
    # must never escape data_dir/stances/. Guards the file-path sink directly.
    bad = {
        "candidate": "example-candidate-a", "topic": "../../ledger",
        "stance": "supports", "summary": "x", "citations": ["e#0"],
        "updated_date": "2026-07-07",
    }
    with pytest.raises(ValueError):
        propose.write_stance(bad, tmp_path)
    assert not (tmp_path / "ledger.json").exists()


def test_write_stance_preserves_an_existing_record(tmp_path):
    # `record` is written by the per-candidate backfill; `write_stance` is called
    # by daily discovery. Since write_stance rewrites the cell wholesale, an
    # unguarded write would silently erase the backfilled record the first time
    # discovery proposed a new position for that candidate+topic — the same class
    # of quiet data loss as the fixed-branch PR clobber, surfacing weeks later.
    existing = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "old", "citations": ["e#0"],
        "updated_date": "2026-07-07",
        "record": [{
            "action": "Bring Chicago Home transfer-tax referendum",
            "outcome": "failed", "date": "2024-03-19", "citations": ["e#0"],
        }],
    }
    propose.write_stance(existing, tmp_path)

    # Discovery proposes a fresh position for the same cell; it carries no record.
    updated = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "mixed", "summary": "new", "citations": ["e#1"],
        "updated_date": "2026-08-19",
    }
    path = propose.write_stance(updated, tmp_path)

    written = json.loads(path.read_text())
    assert written["summary"] == "new"          # position updated
    assert written["stance"] == "mixed"
    assert written["record"] == existing["record"]  # record survived


def test_write_stance_lets_an_explicit_record_win(tmp_path):
    # Preserving must not make the field un-editable: a caller that deliberately
    # supplies `record` (the backfill) overwrites what is on disk.
    first = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "x", "citations": ["e#0"],
        "updated_date": "2026-07-07",
        "record": [{"action": "old", "outcome": "stalled", "citations": ["e#0"]}],
    }
    propose.write_stance(first, tmp_path)

    second = dict(first, record=[
        {"action": "new", "outcome": "enacted", "citations": ["e#1"]},
    ])
    path = propose.write_stance(second, tmp_path)

    assert json.loads(path.read_text())["record"] == second["record"]


def test_write_stance_unions_citations_with_an_existing_cell(tmp_path):
    """Two sources supporting one position must both stay cited.

    `propose_stance_updates` runs per evidence file and cites only that file's
    best statement, so a candidate+topic backed by several media hits is written
    one file at a time. Replacing `citations` on each write means the cell ends up
    citing whichever file happened to be processed last — the earlier source is
    silently dropped even though its evidence record is still committed. Found
    completing the giannoulias backfill (#70): a WTTW citation already merged to
    main disappeared the moment the CBS row was processed.
    """
    first = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "from wttw", "citations": ["wttw-hit#0"],
        "updated_date": "2026-08-02",
    }
    propose.write_stance(first, tmp_path)

    second = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "from cbs", "citations": ["cbs-hit#0"],
        "updated_date": "2026-09-01",
    }
    path = propose.write_stance(second, tmp_path)

    written = json.loads(path.read_text())
    assert written["citations"] == ["wttw-hit#0", "cbs-hit#0"], "both sources cited, oldest first"
    # The newest proposal still owns the position itself.
    assert written["summary"] == "from cbs"
    assert written["updated_date"] == "2026-09-01"


def test_write_stance_does_not_duplicate_a_citation_on_re_run(tmp_path):
    """Re-running a row is routine (~$0.0006); it must be idempotent."""
    stance = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "s", "citations": ["wttw-hit#0"],
        "updated_date": "2026-08-02",
    }
    propose.write_stance(stance, tmp_path)
    path = propose.write_stance(dict(stance, updated_date="2026-09-01"), tmp_path)

    assert json.loads(path.read_text())["citations"] == ["wttw-hit#0"]


def test_write_stance_replaces_a_citation_from_the_same_evidence_file(tmp_path):
    """Union across sources, replace within one — or a re-run can dangle.

    Extraction is not reproducible run-to-run (observed live: the same article
    yielded 0, 2 and 3 statements on separate runs at temperature 0), and a
    citation pins a statement *index*. Accumulating indexes from the same
    evidence id would keep `hit#2` after a re-run that produced only two
    statements, and a dangling citation fails the data-integrity tests.
    """
    propose.write_stance({
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "first pass", "citations": ["cbs-hit#2"],
        "updated_date": "2026-08-02",
    }, tmp_path)

    # A re-run of the same source now finds its best statement at index 0.
    path = propose.write_stance({
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "re-run", "citations": ["cbs-hit#0"],
        "updated_date": "2026-09-01",
    }, tmp_path)

    assert json.loads(path.read_text())["citations"] == ["cbs-hit#0"], \
        "the stale index from the same evidence file must not survive"
