"""Find new candidate media, dedupe it, and triage it for relevance.

* ``parse_feed`` — RSS/Atom -> list of items (feedparser).
* ``Ledger`` — remembers URLs already processed so each item is handled once.
* ``website_changed`` — content-hash diff for pages without a feed.
* ``triage`` — one cheap LLM call: is this item worth ingesting at all?
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

TRIAGE_SYSTEM = (
    "You are a relevance filter for a Chicago mayoral housing tracker. "
    "Given a headline/summary, decide whether it plausibly features a mayoral "
    "candidate discussing policy (especially housing). Respond as JSON: "
    '{"relevant": true|false, "reason": "..."}. When unsure, lean relevant.'
)


# A feed declares its media type via its `type`; discovery routes ingestion on
# this instead of hardcoding "article" (which sent YouTube/podcast items down the
# text path). Types not listed here (google-news, rss) are plain articles.
_FEED_TYPE_TO_MEDIA_TYPE = {
    "youtube": "youtube",
    "podcast": "podcast",
    "bluesky": "social",
    "website": "website",
}


def media_type_for_feed(feed: dict) -> str:
    return _FEED_TYPE_TO_MEDIA_TYPE.get(feed.get("type"), "article")


def parse_feed(feed_text: str, *, source_id: str, prefer_enclosure: bool = False) -> list[dict]:
    import feedparser

    parsed = feedparser.parse(feed_text)
    items = []
    for entry in parsed.entries:
        url = entry.get("link", "")
        if prefer_enclosure:
            # Podcast items: the audio lives in <enclosure>, not the episode page —
            # yt-dlp/Groq need the media file. Fall back to <link> if none.
            enclosures = entry.get("enclosures") or []
            if enclosures and enclosures[0].get("href"):
                url = enclosures[0]["href"]
        items.append({
            "url": url,
            "title": entry.get("title", ""),
            "published": entry.get("published", ""),
            "source_id": source_id,
        })
    return items


class Ledger:
    """Set of already-seen URLs, persisted as JSON."""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self._seen = set(json.loads(self.path.read_text()).get("seen", []))
        else:
            self._seen = set()

    def is_new(self, url: str) -> bool:
        return url not in self._seen

    def filter_new(self, items: list[dict]) -> list[dict]:
        return [it for it in items if self.is_new(it["url"])]

    def mark(self, url: str) -> None:
        self._seen.add(url)

    def mark_all(self, items: list[dict]) -> None:
        for it in items:
            self.mark(it["url"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"seen": sorted(self._seen)}, indent=2))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def website_changed(url: str, html: str, cache: dict) -> bool:
    """True if the page content differs from the last time we saw it.

    ``cache`` maps url -> content hash and is mutated in place. First sighting
    counts as changed (new content to consider).
    """
    digest = _hash(html)
    changed = cache.get(url) != digest
    cache[url] = digest
    return changed


def triage(headline_or_summary: str, *, llm, model: str) -> bool:
    verdict = llm.complete_json(
        model=model,
        system=TRIAGE_SYSTEM,
        user=headline_or_summary,
    )
    return bool(verdict.get("relevant"))


# Feed types discovery actually polls. `website` is intentionally excluded
# (website-diff was descoped); a `website` feed left enabled would otherwise
# silently contribute nothing.
_POLLED_FEED_TYPES = {"rss", "google-news", "youtube", "podcast", "bluesky"}


def active_media_feeds(feeds, *, google_news_enabled: bool = True) -> list[dict]:
    """Select the feeds discovery should poll from a full feed list.

    Drops types discovery doesn't poll (e.g. ``website``) and, when
    ``google_news_enabled`` is false, drops ``google-news`` feeds: their item
    links are ``news.google.com`` redirects that a plain fetch can't read (it
    gets Google's JS interstitial), so polling them only burns the item budget
    and triage cost. The feed data is kept so a headless-fetch path (#30) can
    re-enable them by flipping the flag.
    """
    out = []
    for f in feeds:
        if f.get("type") not in _POLLED_FEED_TYPES:
            continue
        if f.get("type") == "google-news" and not google_news_enabled:
            continue
        out.append(f)
    return out


@dataclass
class DiscoveryResult:
    bodies: list = field(default_factory=list)  # PR-body text per housing item
    feeds_polled: int = 0
    feeds_failed: int = 0
    items_new: int = 0
    triaged_out: int = 0
    ingested: int = 0     # items ingested + extracted successfully (non-empty)
    housing_hits: int = 0
    skipped: int = 0      # items that raised during ingest/extract (left un-marked)


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr)


def run_discovery(feeds, *, ledger, item_fetcher, triage_fn, process_fn,
                  max_items: int, max_items_per_feed: int | None = None,
                  log=None) -> DiscoveryResult:
    """Poll ``feeds``, triage + process new items, and record what happened.

    Injected seams (so this is fully offline-testable):
      * ``item_fetcher(feed) -> list[item]`` — feed XML/JSON to item dicts
        (``{"url", "title", ...}``); may raise (the feed is then skipped).
      * ``triage_fn(title) -> bool`` — cheap relevance filter.
      * ``process_fn(feed, item) -> ProcessResult`` — ingest→extract→propose for
        one item; may raise on a hard ingest/extract failure.

    Ledger policy (the key hardening): an item is marked *seen* when it is
    processed successfully **or** definitively triaged out — but **not** when
    ``process_fn`` raises, so a transient failure (a blocked fetch, the YouTube
    bot-gate) retries next run instead of being burned forever. A global
    ``max_items`` bounds cost/PR size; an optional ``max_items_per_feed`` keeps
    one noisy feed from starving later high-signal feeds (podcasts, Bluesky).
    """
    log = log or _stderr_log
    res = DiscoveryResult()

    for feed in feeds:
        if res.ingested >= max_items:
            log(f"reached max_items={max_items}; remaining feeds deferred to next run")
            break
        feed_id = feed.get("id") or feed.get("name") or feed.get("url", "?")
        try:
            items = item_fetcher(feed)
        except Exception as e:  # noqa: BLE001 — one bad feed shouldn't sink the run
            res.feeds_failed += 1
            log(f"skip feed {feed_id}: {e}")
            continue
        res.feeds_polled += 1
        new_items = ledger.filter_new(items)
        res.items_new += len(new_items)
        log(f"feed {feed_id}: {len(items)} items ({len(new_items)} new)")

        feed_ingested = 0
        for item in new_items:
            if res.ingested >= max_items:
                log(f"reached max_items={max_items}; remaining items deferred to next run")
                break
            if max_items_per_feed is not None and feed_ingested >= max_items_per_feed:
                log(f"feed {feed_id}: hit per-feed cap {max_items_per_feed}; "
                    f"remaining items deferred to next run")
                break
            if not triage_fn(item["title"]):
                ledger.mark(item["url"])  # decided not relevant — don't re-triage daily
                res.triaged_out += 1
                continue
            try:
                result = process_fn(feed, item)
            except Exception as e:  # noqa: BLE001 — transient; leave un-marked to retry
                res.skipped += 1
                log(f"skip item {item['url']}: {e}")
                continue
            ledger.mark(item["url"])  # ingested successfully -> seen
            res.ingested += 1
            feed_ingested += 1
            log(f"ingest {item['url']}: {getattr(result, 'transcript_chars', 0)} chars, "
                f"housing={result.housing_count}")
            if result.housing_count:
                res.bodies.append(result.pr_body)
                res.housing_hits += 1

    ledger.save()
    log(f"discovery: feeds polled={res.feeds_polled} failed={res.feeds_failed} "
        f"new_items={res.items_new} triaged_out={res.triaged_out} "
        f"ingested={res.ingested} housing={res.housing_hits} skipped={res.skipped}")
    return res
