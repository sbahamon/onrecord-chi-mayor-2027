"""Discovery finds new candidate media and filters it down to what's relevant.

Feed parsing, dedup against the ledger, and website-change detection are pure
and tested directly. Triage (is this item actually about a tracked candidate's
policy?) uses an injected fake LLM.
"""
import json
from pathlib import Path

from pipeline import discover

FIXTURES = Path(__file__).parent / "fixtures"

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


def test_parse_feed_uses_enclosure_url_for_podcasts():
    # For a podcast feed the item url must be the audio ENCLOSURE (what yt-dlp/Groq
    # ingest), not the episode webpage. Guarded by prefer_enclosure so only podcasts
    # get this treatment.
    xml = (FIXTURES / "podcast.xml").read_text()
    items = discover.parse_feed(xml, source_id="pod", prefer_enclosure=True)
    assert items[0]["url"] == "https://cdn.example.com/audio/doe-housing.mp3"
    assert items[0]["title"] == "Mayoral candidate Doe on housing"
    assert items[1]["url"] == "https://cdn.example.com/audio/budget-recap.mp3"


def test_parse_feed_default_keeps_link_not_enclosure():
    # Backward compatibility: without prefer_enclosure (article/news/youtube feeds),
    # the item url stays the <link>, even if an enclosure is present.
    xml = (FIXTURES / "podcast.xml").read_text()
    items = discover.parse_feed(xml, source_id="pod")
    assert items[0]["url"] == "https://example.com/episodes/doe-housing"


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


# --- active_media_feeds (poll-time feed selection) --------------------------

def test_active_media_feeds_filters_unsupported_types():
    feeds = [
        {"id": "a", "name": "A", "type": "rss", "url": "https://a"},
        {"id": "b", "name": "B", "type": "podcast", "url": "https://b"},
        {"id": "c", "name": "C", "type": "website", "url": "https://c"},  # not polled
        {"id": "d", "name": "D", "type": "bluesky", "url": "d.bsky.social"},
    ]
    got = [f["id"] for f in discover.active_media_feeds(feeds)]
    assert got == ["a", "b", "d"]  # website dropped


def test_active_media_feeds_gates_google_news():
    feeds = [
        {"id": "gn", "name": "GN", "type": "google-news", "url": "https://news.google.com/x"},
        {"id": "rs", "name": "RS", "type": "rss", "url": "https://outlet/feed"},
    ]
    # Google News redirect URLs are unreadable without a headless render, so they
    # can be gated off at poll time (data kept for when headless lands).
    off = [f["id"] for f in discover.active_media_feeds(feeds, google_news_enabled=False)]
    assert off == ["rs"]
    on = [f["id"] for f in discover.active_media_feeds(feeds, google_news_enabled=True)]
    assert on == ["gn", "rs"]


# --- run_discovery (the orchestration loop) ---------------------------------

class _FakeResult:
    def __init__(self, *, housing_count=0, transcript_chars=500, pr_body="body"):
        self.housing_count = housing_count
        self.transcript_chars = transcript_chars
        self.pr_body = pr_body


def _items(*urls):
    return [{"url": u, "title": f"title {u}"} for u in urls]


def test_run_discovery_marks_success_and_triage_reject_but_not_failure(tmp_path):
    # The core hardening: a triaged-out item is marked seen (decision made, don't
    # re-triage daily); a SUCCESSFUL item is marked; a FAILED ingest is left
    # un-marked so it retries next run (the old code marked every item up-front,
    # so a transient failure burned the URL forever).
    ledger = discover.Ledger(tmp_path / "ledger.json")
    feed = {"id": "f", "name": "F", "type": "rss", "url": "https://f"}

    def item_fetcher(_feed):
        return _items("https://ok", "https://irrelevant", "https://boom")

    def triage_fn(title):
        return "irrelevant" not in title

    def process_fn(_feed, item):
        if item["url"] == "https://boom":
            raise RuntimeError("ingest blew up")
        return _FakeResult(housing_count=1, pr_body="PR for ok")

    res = discover.run_discovery(
        [feed], ledger=ledger, item_fetcher=item_fetcher,
        triage_fn=triage_fn, process_fn=process_fn, max_items=25, log=lambda m: None,
    )

    assert res.ingested == 1
    assert res.triaged_out == 1
    assert res.skipped == 1
    assert res.housing_hits == 1
    assert res.bodies == ["PR for ok"]

    reloaded = discover.Ledger(tmp_path / "ledger.json")
    assert not reloaded.is_new("https://ok")          # success -> marked
    assert not reloaded.is_new("https://irrelevant")  # triaged out -> marked
    assert reloaded.is_new("https://boom")            # failed -> NOT marked, retries


def test_run_discovery_respects_global_max_items(tmp_path):
    ledger = discover.Ledger(tmp_path / "ledger.json")
    feed = {"id": "f", "name": "F", "type": "rss", "url": "https://f"}

    def item_fetcher(_feed):
        return _items(*[f"https://n{i}" for i in range(10)])

    res = discover.run_discovery(
        [feed], ledger=ledger, item_fetcher=item_fetcher,
        triage_fn=lambda t: True, process_fn=lambda f, i: _FakeResult(),
        max_items=3, log=lambda m: None,
    )
    assert res.ingested == 3  # capped


def test_run_discovery_per_feed_cap_leaves_budget_for_later_feeds(tmp_path):
    # A noisy first feed must not starve a later (high-signal) feed: a per-feed cap
    # lets the second feed still be polled within the global budget.
    ledger = discover.Ledger(tmp_path / "ledger.json")
    noisy = {"id": "noisy", "name": "N", "type": "rss", "url": "https://n"}
    podcast = {"id": "pod", "name": "P", "type": "podcast", "url": "https://p"}

    def item_fetcher(feed):
        if feed["id"] == "noisy":
            return _items(*[f"https://n{i}" for i in range(10)])
        return _items("https://pod-ep1")

    seen_feeds = []

    def process_fn(feed, item):
        seen_feeds.append(feed["id"])
        return _FakeResult()

    res = discover.run_discovery(
        [noisy, podcast], ledger=ledger, item_fetcher=item_fetcher,
        triage_fn=lambda t: True, process_fn=process_fn,
        max_items=25, max_items_per_feed=3, log=lambda m: None,
    )
    assert seen_feeds.count("noisy") == 3       # noisy capped at per-feed limit
    assert "pod" in seen_feeds                  # podcast still reached
    assert res.ingested == 4


def test_run_discovery_skips_a_feed_that_fails_to_fetch(tmp_path):
    ledger = discover.Ledger(tmp_path / "ledger.json")
    bad = {"id": "bad", "name": "B", "type": "rss", "url": "https://bad"}
    good = {"id": "good", "name": "G", "type": "rss", "url": "https://good"}

    def item_fetcher(feed):
        if feed["id"] == "bad":
            raise RuntimeError("feed 500")
        return _items("https://good-1")

    res = discover.run_discovery(
        [bad, good], ledger=ledger, item_fetcher=item_fetcher,
        triage_fn=lambda t: True, process_fn=lambda f, i: _FakeResult(housing_count=1),
        max_items=25, log=lambda m: None,
    )
    assert res.feeds_failed == 1
    assert res.feeds_polled == 1
    assert res.ingested == 1


def test_run_discovery_survives_a_triage_failure(tmp_path):
    # A flaky triage call must not sink the whole run. The triage model
    # intermittently returns a non-JSON refusal (observed live: a Chinese-language
    # "I haven't learned how to answer that" string), which `llm.complete_json`
    # surfaces as LLMError. Before this guard that exception propagated out of the
    # loop and killed the run *after* real transcription work — 12 of 30 scheduled
    # runs died that way over a month, and `ledger.save()` (after the loop) never
    # ran, so the run's marks were lost too.
    #
    # The item is left UN-marked, exactly like a failed ingest: a transient LLM
    # failure must retry next run, never burn a URL.
    ledger = discover.Ledger(tmp_path / "ledger.json")
    feed = {"id": "f", "name": "F", "type": "rss", "url": "https://f"}

    def item_fetcher(_feed):
        return _items("https://before", "https://triage-boom", "https://after")

    def triage_fn(title):
        if "triage-boom" in title:
            raise RuntimeError("model did not return valid JSON")
        return True

    res = discover.run_discovery(
        [feed], ledger=ledger, item_fetcher=item_fetcher,
        triage_fn=triage_fn, process_fn=lambda f, i: _FakeResult(housing_count=1),
        max_items=25, log=lambda m: None,
    )

    # The run continues past the bad item and still processes what follows it.
    assert res.ingested == 2
    assert res.skipped == 1

    reloaded = discover.Ledger(tmp_path / "ledger.json")
    assert not reloaded.is_new("https://before")
    assert not reloaded.is_new("https://after")
    assert reloaded.is_new("https://triage-boom")  # un-marked -> retries next run


def test_run_discovery_saves_ledger_even_when_every_triage_fails(tmp_path):
    # The failure mode that made the outage invisible: the whole run aborting meant
    # `ledger.save()` never executed. Even a total triage outage must land the marks
    # already earned and return a result rather than raising.
    ledger = discover.Ledger(tmp_path / "ledger.json")
    feed = {"id": "f", "name": "F", "type": "rss", "url": "https://f"}

    res = discover.run_discovery(
        [feed], ledger=ledger,
        item_fetcher=lambda _f: _items("https://a", "https://b"),
        triage_fn=_raise_llm_error, process_fn=lambda f, i: _FakeResult(),
        max_items=25, log=lambda m: None,
    )

    assert res.ingested == 0
    assert res.skipped == 2
    assert (tmp_path / "ledger.json").exists()  # save() reached


def _raise_llm_error(_title):
    raise RuntimeError("model did not return valid JSON: '作为一个人工智能语言模型…'")
