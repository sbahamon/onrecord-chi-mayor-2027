"""Config/registry loading from the data/ tree."""
from pathlib import Path

from pipeline import config

REPO = Path(__file__).resolve().parent.parent


def test_load_config_returns_models_and_flags():
    cfg = config.load_config(REPO / "data")
    assert set(cfg["models"]) == {"triage", "extractor", "reviewer"}
    assert cfg["auto_merge_enabled"] is False  # ships off


def test_candidate_and_topic_slugs():
    slugs = config.candidate_slugs(REPO / "data")
    assert "brandon-johnson" in slugs
    topics = config.topic_slugs(REPO / "data")
    assert "zoning-reform" in topics


def test_active_candidate_slugs_excludes_examples_and_withdrawn():
    active = config.candidate_slugs(REPO / "data", active_only=True)
    assert "brandon-johnson" in active
    assert "example-candidate-a" not in active
