"""Fetch a candidate's recent Bluesky posts as discovery items (text-only).

Bluesky exposes a public, unauthenticated read API. A post's text IS the content
we extract from — there is no audio path for Bluesky — so this returns the post
text on each item and discovery routes it to the ``social`` media_type, which
``ingest`` treats as a pre-supplied transcript.

Only the candidate's own original text posts survive: reposts (someone else's
words) and media-only posts (no text to extract) are dropped. Embedded photos,
video, and link cards are intentionally ignored (see the discovery-expansion plan).

HTTP is injected (``get``) so the module is testable offline against a fixture.
"""
from __future__ import annotations

import json
from urllib.parse import quote

# Public AppView — no auth needed for public posts.
GET_AUTHOR_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

# A short snippet of the post text, used as the item title (schema needs a
# non-empty title; the full text still flows through as the transcript).
_TITLE_LEN = 120


def _default_get(url: str) -> str:
    import requests

    from pipeline.ingest import BROWSER_USER_AGENT

    resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_USER_AGENT})
    resp.raise_for_status()
    return resp.text


def _post_web_url(handle: str, uri: str) -> str:
    # uri: at://<did>/app.bsky.feed.post/<rkey> -> bsky.app permalink for the author.
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _title_from_text(text: str) -> str:
    snippet = " ".join(text.split())
    return snippet[:_TITLE_LEN].rstrip() or snippet[:_TITLE_LEN]


def fetch_author_feed(handle: str, *, limit: int = 25, get=None) -> list[dict]:
    """Return the candidate's recent original text posts as discovery items.

    Each item: ``{url, title, text, published, source_id}``. ``get(url) -> str``
    is an injected HTTP getter returning the response body (defaults to a real
    browser-UA request).
    """
    get = get or _default_get
    url = f"{GET_AUTHOR_FEED}?actor={quote(handle)}&limit={int(limit)}"
    data = json.loads(get(url))

    items = []
    for entry in data.get("feed", []):
        if entry.get("reason"):
            continue  # a repost — not the candidate's own words
        post = entry.get("post") or {}
        record = post.get("record") or {}
        text = (record.get("text") or "").strip()
        if not text:
            continue  # media-only post: nothing to extract
        items.append({
            "url": _post_web_url(handle, post.get("uri", "")),
            "title": _title_from_text(text),
            "text": text,
            "published": record.get("createdAt", ""),
            "source_id": f"bluesky-{handle}",
        })
    return items
