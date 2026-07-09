"""Turn a raw media source into clean transcript text + an evidence stub.

Network, download, and transcription are injected so the module is testable and
so the heavy tools (requests, yt-dlp, Whisper) live behind seams:

    ingest(source, fetcher=..., downloader=..., transcriber=...)

* text sources (article/website): ``fetcher(url) -> html`` then readability
* caption sources (youtube): ``fetcher(url) -> vtt`` then ``normalize_vtt``
* audio/video (podcast/social/manual): ``downloader(url) -> path`` then
  ``transcriber(path) -> text`` — the media file is caller-managed and discarded

Defaults wire real implementations, imported lazily so unit tests need none.
"""
from __future__ import annotations

import re

# Present a real browser UA: some campaign/outlet sites 403 non-browser agents
# (seen on dannicformayor.com). The reviewer re-ingests the same URL to verify
# quotes, so ingest and review must fetch identically — keep this the single source.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

TEXT_TYPES = {"article", "website"}
# YouTube goes through the audio path (yt-dlp downloads the audio, then Whisper).
# yt-dlp resolves YouTube URLs directly; a prior caption-fetch path was broken.
AUDIO_TYPES = {"podcast", "social", "manual", "youtube"}


def slugify(text: str, *, max_len: int | None = None) -> str:
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if max_len is not None and len(text) > max_len:
        text = text[:max_len].rstrip("-")  # bound length, keep a clean slug boundary
    return text


def make_evidence_id(published_date: str, outlet: str, title: str) -> str:
    # Cap the variable parts: some pages yield a junk multi-hundred-char "title"
    # (e.g. a browser-upgrade notice), which would otherwise produce an evidence id
    # too long to use as a filename. Date + a bounded outlet + a bounded title keep
    # the id readable, still unique enough, and safely under the FS name limit.
    parts = [published_date, slugify(outlet, max_len=40), slugify(title, max_len=70)]
    return "-".join(p for p in parts if p)


def normalize_vtt(vtt: str) -> str:
    """WebVTT/SRT captions -> plain transcript text.

    Drops the header, cue-timing lines, and cue numbers, then collapses the
    rolling duplicate lines typical of auto-generated captions.
    """
    out: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.isdigit():  # SRT cue index
            continue
        if out and out[-1] == line:  # collapse consecutive duplicates
            continue
        out.append(line)
    # Second pass: drop a line identical to the immediately preceding one even
    # across cue boundaries (rolling captions repeat the last line first).
    deduped: list[str] = []
    for line in out:
        if deduped and deduped[-1] == line:
            continue
        deduped.append(line)
    return "\n".join(deduped)


def domain_of(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def extract_article(html: str) -> tuple[str, str]:
    """Return (main_text, title) for an article page."""
    import trafilatura

    data = trafilatura.bare_extraction(
        html, include_comments=False, include_tables=False
    )

    def field(name):
        if data is None:
            return None
        if isinstance(data, dict):
            return data.get(name)
        return getattr(data, name, None)  # trafilatura Document object

    text = field("text") or trafilatura.extract(html) or ""
    title = field("title") or _title_from_html(html)
    return text, title


def _title_from_html(html: str) -> str:
    for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ""


def extract_article_text(html: str) -> str:
    return extract_article(html)[0]


def _preferred_encoding(header_encoding: str | None, apparent_encoding: str | None) -> str:
    # requests defaults to ISO-8859-1 for charset-less text/html, which turns a
    # UTF-8 page into mojibake. When the header charset is absent or that latin-1
    # default, trust the sniffed (apparent) encoding instead.
    if not header_encoding or header_encoding.lower() == "iso-8859-1":
        return apparent_encoding or header_encoding or "utf-8"
    return header_encoding


def _default_fetcher(url: str, *, getter=None) -> str:
    if getter is None:
        import requests

        getter = requests.get
    resp = getter(url, timeout=30, headers={"User-Agent": BROWSER_USER_AGENT})
    resp.raise_for_status()
    resp.encoding = _preferred_encoding(resp.encoding, resp.apparent_encoding)
    return resp.text


# Below this, trafilatura almost certainly saw a JS shell, not the article body,
# so a headless render (which executes client-side JS) is worth trying.
MIN_ARTICLE_CHARS = 200


def ingest(source: dict, *, fetcher=None, downloader=None, transcriber=None,
           headless_fetcher=None) -> dict:
    media_type = source["media_type"]
    fetcher = fetcher or _default_fetcher
    title = source.get("title")

    if media_type in TEXT_TYPES:
        transcript, page_title = extract_article(fetcher(source["url"]))
        if headless_fetcher is not None and len(transcript.strip()) < MIN_ARTICLE_CHARS:
            # Plain fetch yielded little/no text — likely JS-rendered. Re-fetch
            # with a headless render so both ingest and the reviewer can read it.
            transcript, page_title = extract_article(headless_fetcher(source["url"]))
        if not title:
            title = page_title or source["url"]
    elif media_type in AUDIO_TYPES:
        if downloader is None or transcriber is None:
            from pipeline.transcribe import download_media, transcribe_audio

            downloader = downloader or download_media
            transcriber = transcriber or transcribe_audio
        transcript = transcriber(downloader(source["url"]))
    else:
        raise ValueError(f"unknown media_type: {media_type!r}")

    if not title:
        title = source["url"]

    return {
        "id": make_evidence_id(source["published_date"], source["outlet"], title),
        "url": source["url"],
        "outlet": source["outlet"],
        "media_type": media_type,
        "title": title,
        "published_date": source["published_date"],
        "transcript": transcript,
    }
