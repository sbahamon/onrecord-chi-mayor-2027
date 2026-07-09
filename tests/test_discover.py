"""Discovery finds new candidate media and filters it down to what's relevant.

Feed parsing, dedup against the ledger, and website-change detection are pure
and tested directly. Triage (is this item actually about a tracked candidate's
policy?) uses an injected fake LLM.
"""
import json

from pipeline import discover

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Feed</title>
  <item>
    <title>Doe talks housing on WBEZ</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 06 Jul 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>City council recap</title>
    <link>https://example.com/b</link>
    <pubDate>Sun, 05 Jul 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_parse_feed_extracts_items():
    items = discover.parse_feed(RSS, source_id="example")
    assert len(items) == 2
    assert items[0]["url"] == "https://example.com/a"
    assert items[0]["title"] == "Doe talks housing on WBEZ"
    assert items[0]["source_id"] == "example"


def test_media_type_for_feed_maps_by_feed_type():
    # The feed declares its media type; discovery routes on it instead of
    # hardcoding "article" (which sent YouTube/podcast items down the wrong path).
    assert discover.media_type_for_feed({"type": "youtube"}) == "youtube"
    assert discover.media_type_for_feed({"type": "podcast"}) == "podcast"
    assert discover.media_type_for_feed({"type": "bluesky"}) == "social"
    assert discover.media_type_for_feed({"type": "website"}) == "website"
    assert discover.media_type_for_feed({"type": "google-news"}) == "article"
    assert discover.media_type_for_feed({"type": "rss"}) == "article"


def test_ledger_dedupe_filters_seen_urls(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = discover.Ledger(ledger_path)
    items = discover.parse_feed(RSS, source_id="example")

    first = ledger.filter_new(items)
    assert len(first) == 2
    ledger.mark_all(first)
    ledger.save()

    # Reload from disk; the same items are now all seen.
    reloaded = discover.Ledger(ledger_path)
    second = reloaded.filter_new(items)
    assert second == []


def test_website_changed_detects_content_change():
    cache = {}
    url = "https://example.com/politics"
    assert discover.website_changed(url, "<p>v1</p>", cache) is True
    assert discover.website_changed(url, "<p>v1</p>", cache) is False  # unchanged
    assert discover.website_changed(url, "<p>v2</p>", cache) is True   # changed


class FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict

    def complete_json(self, *, model, system, user):
        return self.verdict


def test_triage_true_when_model_says_relevant():
    llm = FakeLLM({"relevant": True, "reason": "Doe discusses zoning"})
    assert discover.triage("Doe talks housing", llm=llm, model="m") is True


def test_triage_false_when_model_says_irrelevant():
    llm = FakeLLM({"relevant": False, "reason": "unrelated council procedure"})
    assert discover.triage("City council recap", llm=llm, model="m") is False
