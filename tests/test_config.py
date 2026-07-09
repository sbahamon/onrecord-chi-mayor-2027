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


def test_discovery_feeds_merge_sources_and_candidate_rss():
    feeds = config.discovery_feeds(REPO / "data")
    urls = [f["url"] for f in feeds]
    # Includes the shared source feeds...
    assert any("news.google.com/rss/search" in u for u in urls)
    # ...and a per-candidate feed for an active candidate with google_news_rss.
    assert any("Mendoza" in u or "Mendoza".replace(" ", "%20") in u for u in urls)
    # Every feed is a well-formed discovery feed entry.
    for f in feeds:
        assert {"id", "name", "type", "url"} <= set(f)


def test_discovery_feeds_skip_candidates_without_rss_and_disabled_sources():
    feeds = config.discovery_feeds(REPO / "data")
    # No candidate entry should have an empty URL.
    assert all(f["url"] for f in feeds)
