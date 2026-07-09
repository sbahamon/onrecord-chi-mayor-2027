"""Find new candidate media, dedupe it, and triage it for relevance.

* ``parse_feed`` — RSS/Atom -> list of items (feedparser).
* ``Ledger`` — remembers URLs already processed so each item is handled once.
* ``website_changed`` — content-hash diff for pages without a feed.
* ``triage`` — one cheap LLM call: is this item worth ingesting at all?
"""
from __future__ import annotations

import hashlib
import json
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
