"""Citations link a curated stance back to the exact statement that supports it.

A citation is ``"<evidence-id>#<statement-index>"``. The resolver turns that
into the statement dict, and is the mechanism the site uses to show the quote
and source behind every matrix cell. A dangling citation must be detectable.
"""
import pytest

from pipeline.citations import parse_citation, resolve_citation, CitationError

EVIDENCE_INDEX = {
    "2026-07-06-example-forum": {
        "id": "2026-07-06-example-forum",
        "url": "https://example.com/x",
        "statements": [
            {"candidate": "a", "quote": "first"},
            {"candidate": "b", "quote": "second"},
        ],
    }
}


def test_parse_citation_splits_id_and_index():
    assert parse_citation("2026-07-06-example-forum#1") == ("2026-07-06-example-forum", 1)


def test_parse_citation_rejects_missing_index():
    with pytest.raises(CitationError):
        parse_citation("2026-07-06-example-forum")


def test_resolve_citation_returns_the_statement_and_evidence():
    stmt, evidence = resolve_citation("2026-07-06-example-forum#1", EVIDENCE_INDEX)
    assert stmt["quote"] == "second"
    assert evidence["url"] == "https://example.com/x"


def test_resolve_citation_unknown_evidence_raises():
    with pytest.raises(CitationError):
        resolve_citation("no-such-id#0", EVIDENCE_INDEX)


def test_resolve_citation_index_out_of_range_raises():
    with pytest.raises(CitationError):
        resolve_citation("2026-07-06-example-forum#5", EVIDENCE_INDEX)


def test_every_committed_stance_citation_resolves():
    """Guardrail: no dangling citations in real data/."""
    import json
    from pathlib import Path

    from pipeline.data_integrity import iter_data_files

    repo = Path(__file__).resolve().parent.parent
    evidence_index = {}
    for path, schema in iter_data_files(repo / "data"):
        if schema == "evidence":
            doc = json.loads(path.read_text())
            evidence_index[doc["id"]] = doc

    for path, schema in iter_data_files(repo / "data"):
        if schema != "stance":
            continue
        doc = json.loads(path.read_text())
        for citation in doc["citations"]:
            resolve_citation(citation, evidence_index)  # raises if dangling
