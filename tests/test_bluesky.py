"""Bluesky (text-only social) discovery client.

The client hits the public getAuthorFeed API (no auth). HTTP is injected so tests
run offline against a fixture. Only the candidate's own original posts with text
survive: reposts and media-only (empty text) posts are dropped, because a text
social post's text IS the transcript (no audio path for Bluesky).
"""
from pathlib import Path

from pipeline import bluesky

FIXTURES = Path(__file__).parent / "fixtures"
FEED = (FIXTURES / "bluesky_feed.json").read_text()


def test_fetch_author_feed_returns_original_text_posts_as_items():
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return FEED

    items = bluesky.fetch_author_feed("cand.bsky.social", get=fake_get)

    # queried the right actor
    assert "getAuthorFeed" in captured["url"]
    assert "cand.bsky.social" in captured["url"]

    # media-only (empty text) and the repost are dropped; one real post remains
    assert len(items) == 1
    it = items[0]
    assert it["text"].startswith("We must legalize apartments")
    assert it["title"]  # a non-empty snippet for triage/evidence title
    # the item url is the human bsky.app permalink for that post
    assert it["url"] == "https://bsky.app/profile/cand.bsky.social/post/3lstextpost"


def test_fetch_author_feed_skips_reposts_and_media_only():
    items = bluesky.fetch_author_feed("cand.bsky.social", get=lambda url: FEED)
    urls = [it["url"] for it in items]
    assert "3lsrepost" not in " ".join(urls)          # not the candidate's words
    assert "3lsmediapost" not in " ".join(urls)       # no text to extract from
