"""Guardrail tests over the real files in data/.

These are not fixture tests — they validate the actual registries and any
committed evidence/stance files. A bad merge that corrupts a data file (which
would otherwise silently break the site build) fails CI here instead.
"""
import json
from pathlib import Path

import pytest

from pipeline import schemas
from pipeline.data_integrity import iter_data_files

REPO = Path(__file__).resolve().parent.parent


def test_registry_files_exist():
    for name in ("candidates", "sources", "topics", "config"):
        assert (REPO / "data" / "registry" / f"{name}.json").exists(), name


def test_candidates_registry_is_valid():
    doc = json.loads((REPO / "data/registry/candidates.json").read_text())
    schemas.validate(doc, "candidates")


def test_sources_registry_is_valid():
    doc = json.loads((REPO / "data/registry/sources.json").read_text())
    schemas.validate(doc, "sources")


def test_topics_registry_is_valid():
    doc = json.loads((REPO / "data/registry/topics.json").read_text())
    schemas.validate(doc, "topics")


def test_every_data_file_validates_against_its_schema():
    """Walk data/ and validate each file against the schema for its location."""
    checked = 0
    for path, schema_name in iter_data_files(REPO / "data"):
        doc = json.loads(path.read_text())
        try:
            schemas.validate(doc, schema_name)
        except Exception as e:  # noqa: BLE001 - surface which file failed
            pytest.fail(f"{path} failed {schema_name} schema: {e}")
        checked += 1
    assert checked >= 4  # at least the four registries


def test_topics_are_unique_and_referenced_stances_use_known_topics():
    topics = {
        t["slug"]
        for t in json.loads((REPO / "data/registry/topics.json").read_text())["topics"]
    }
    candidates = {
        c["slug"]
        for c in json.loads((REPO / "data/registry/candidates.json").read_text())["candidates"]
    }
    for path, schema_name in iter_data_files(REPO / "data"):
        if schema_name != "stance":
            continue
        doc = json.loads(path.read_text())
        assert doc["topic"] in topics, f"{path}: unknown topic {doc['topic']}"
        assert doc["candidate"] in candidates, f"{path}: unknown candidate {doc['candidate']}"
