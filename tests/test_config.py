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


def test_discovery_feeds_emits_per_candidate_youtube_feed():
    # A tracked candidate with a youtube_channel gets a per-candidate YouTube feed
    # (channel RSS), routed to the audio path by the media-type mapper.
    feeds = config.discovery_feeds(REPO / "data")
    yt = [f for f in feeds
          if f["type"] == "youtube" and f["id"] == "candidate-brandon-johnson-youtube"]
    assert len(yt) == 1
    assert "www.youtube.com/feeds/videos.xml?channel_id=" in yt[0]["url"]
    assert yt[0]["url"].endswith("UCjGcYJCcXiC0epFgVKSv-mQ")


def test_discovery_feeds_skip_youtube_for_candidates_without_channel():
    feeds = config.discovery_feeds(REPO / "data")
    yt_ids = [f["id"] for f in feeds if f["type"] == "youtube"]
    # toni-brooks has no confirmed youtube_channel -> no per-candidate youtube feed.
    assert "candidate-toni-brooks-youtube" not in yt_ids
    # A dropped (tracked:false) candidate is never emitted.
    assert "candidate-danielle-carter-walters-youtube" not in yt_ids


def test_discovery_feeds_emits_per_candidate_bluesky_feed():
    # A tracked candidate with a bluesky handle gets a per-candidate Bluesky feed;
    # the feed url is the handle (the bluesky client resolves it to getAuthorFeed).
    feeds = config.discovery_feeds(REPO / "data")
    bs = [f for f in feeds
          if f["type"] == "bluesky" and f["id"] == "candidate-brandon-johnson-bluesky"]
    assert len(bs) == 1
    assert bs[0]["url"] == "brandon4chicago.bsky.social"
    # carries the owning candidate so discovery can scope extraction to them
    # (a first-person social post has no name for the extractor to attribute).
    assert bs[0]["candidate"] == "brandon-johnson"


def test_discovery_feeds_skip_bluesky_for_candidates_without_handle():
    feeds = config.discovery_feeds(REPO / "data")
    bs_ids = [f["id"] for f in feeds if f["type"] == "bluesky"]
    # matthew-brewer has no confirmed bluesky handle -> no per-candidate bluesky feed.
    assert "candidate-matthew-brewer-bluesky" not in bs_ids
    assert "candidate-danielle-carter-walters-bluesky" not in bs_ids


def test_dropped_candidate_is_excluded_everywhere():
    # A `tracked: false` candidate (danielle-carter-walters) is not processed:
    # not in the extractor's slug list, not polled by discovery.
    slugs = config.candidate_slugs(REPO / "data")
    active = config.candidate_slugs(REPO / "data", active_only=True)
    assert "danielle-carter-walters" not in slugs
    assert "danielle-carter-walters" not in active
    feed_ids = [f["id"] for f in config.discovery_feeds(REPO / "data")]
    assert "candidate-danielle-carter-walters" not in feed_ids
