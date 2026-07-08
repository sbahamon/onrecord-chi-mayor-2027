"""Tests for JSON Schema validation of data files.

The pipeline writes data files (evidence, stances, registries) that the site
build reads. A malformed file would break the build or corrupt the site, so
every data file is validated against a schema. These tests pin the schemas'
behavior: what a valid record looks like, and which malformations are rejected.
"""
import pytest
from jsonschema.exceptions import ValidationError

from pipeline import schemas


# --- a minimal valid evidence record ---------------------------------------

def valid_evidence():
    return {
        "id": "2026-07-06-example-podcast-doe",
        "url": "https://example.com/ep1",
        "outlet": "Example Podcast",
        "media_type": "podcast",
        "title": "Candidate Doe on housing",
        "published_date": "2026-07-06",
        "discovered_date": "2026-07-07",
        "transcript_ref": "data/transcripts/2026-07-06-example-podcast-doe.md",
        "statements": [
            {
                "candidate": "jane-doe",
                "topic": "zoning-reform",
                "stance": "supports",
                "summary": "Backs eliminating single-family-only zoning citywide.",
                "quote": "We should legalize apartments in every neighborhood.",
                "locator": "41:32",
                "confidence": 0.9,
                "is_housing": True,
                "attribution_flag": False,
            }
        ],
    }


def test_valid_evidence_record_passes():
    schemas.validate(valid_evidence(), "evidence")  # must not raise


def test_evidence_missing_required_field_is_rejected():
    record = valid_evidence()
    del record["url"]
    with pytest.raises(ValidationError):
        schemas.validate(record, "evidence")


def test_evidence_bad_stance_enum_is_rejected():
    record = valid_evidence()
    record["statements"][0]["stance"] = "kinda-likes-it"
    with pytest.raises(ValidationError):
        schemas.validate(record, "evidence")


def test_evidence_bad_date_format_is_rejected():
    record = valid_evidence()
    record["published_date"] = "July 6, 2026"
    with pytest.raises(ValidationError):
        schemas.validate(record, "evidence")


def test_evidence_confidence_out_of_range_is_rejected():
    record = valid_evidence()
    record["statements"][0]["confidence"] = 1.5
    with pytest.raises(ValidationError):
        schemas.validate(record, "evidence")


# --- stance (curated matrix cell) -------------------------------------------

def valid_stance():
    return {
        "candidate": "jane-doe",
        "topic": "zoning-reform",
        "stance": "supports",
        "summary": "Backs eliminating single-family-only zoning citywide.",
        "citations": ["2026-07-06-example-podcast-doe#0"],
        "updated_date": "2026-07-07",
    }


def test_valid_stance_record_passes():
    schemas.validate(valid_stance(), "stance")


def test_stance_requires_at_least_one_citation():
    record = valid_stance()
    record["citations"] = []
    with pytest.raises(ValidationError):
        schemas.validate(record, "stance")


# --- unknown schema name ----------------------------------------------------

def test_unknown_schema_name_raises():
    with pytest.raises(KeyError):
        schemas.validate({}, "not-a-real-schema")


def test_statement_schema_matches_evidence_inline_definition():
    """The standalone statement schema and evidence's inline copy must not drift."""
    import json

    standalone = json.loads((schemas.SCHEMA_DIR / "statement.schema.json").read_text())
    evidence = json.loads((schemas.SCHEMA_DIR / "evidence.schema.json").read_text())
    inline = evidence["$defs"]["statement"]
    assert standalone["required"] == inline["required"]
    assert standalone["properties"] == inline["properties"]
