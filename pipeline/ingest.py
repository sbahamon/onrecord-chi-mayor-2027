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

TEXT_TYPES = {"article", "website"}
CAPTION_TYPES = {"youtube"}
AUDIO_TYPES = {"podcast", "social", "manual"}


def slugify(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def make_evidence_id(published_date: str, outlet: str, title: str) -> str:
    parts = [published_date, slugify(outlet), slugify(title)]
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


def extract_article_text(html: str) -> str:
    import trafilatura

    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    return text or ""


def _default_fetcher(url: str) -> str:
    import requests

    resp = requests.get(url, timeout=30, headers={"User-Agent": "housing-tracker/0.1"})
    resp.raise_for_status()
    return resp.text


def ingest(source: dict, *, fetcher=None, downloader=None, transcriber=None) -> dict:
    media_type = source["media_type"]
    fetcher = fetcher or _default_fetcher

    if media_type in TEXT_TYPES:
        transcript = extract_article_text(fetcher(source["url"]))
    elif media_type in CAPTION_TYPES:
        transcript = normalize_vtt(fetcher(source["url"]))
    elif media_type in AUDIO_TYPES:
        if downloader is None or transcriber is None:
            from pipeline.transcribe import download_media, transcribe_audio

            downloader = downloader or download_media
            transcriber = transcriber or transcribe_audio
        transcript = transcriber(downloader(source["url"]))
    else:
        raise ValueError(f"unknown media_type: {media_type!r}")

    return {
        "id": make_evidence_id(source["published_date"], source["outlet"], source["title"]),
        "url": source["url"],
        "outlet": source["outlet"],
        "media_type": media_type,
        "title": source["title"],
        "published_date": source["published_date"],
        "transcript": transcript,
    }
