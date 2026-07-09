"""Ingestion normalizes raw media into clean transcript text + metadata.

Pure, breakable pieces are tested directly (caption parsing, article-body
extraction, id/slug generation). The orchestrator is tested with injected fakes
for the network/transcription so no keys or downloads are needed.
"""
from pathlib import Path

from pipeline import ingest

FIXTURES = Path(__file__).parent / "fixtures"


# --- slug / id --------------------------------------------------------------

def test_slugify_basic():
    assert ingest.slugify("The Ben Joravsky Show!") == "the-ben-joravsky-show"


def test_slugify_collapses_and_trims():
    assert ingest.slugify("  Doe &  Roe:  Housing  ") == "doe-roe-housing"


def test_make_evidence_id_is_date_prefixed_and_slugged():
    got = ingest.make_evidence_id("2026-07-06", "The Ben Joravsky Show", "Johnson interview")
    assert got.startswith("2026-07-06-")
    assert got == "2026-07-06-the-ben-joravsky-show-johnson-interview"


def test_preferred_encoding_trusts_sniffed_when_header_is_latin1_default():
    # requests falls back to ISO-8859-1 for charset-less text/html, which turns a
    # UTF-8 page into mojibake (e.g. cardenas4chicago.com). Trust the sniffed value.
    assert ingest._preferred_encoding("ISO-8859-1", "utf-8") == "utf-8"


def test_preferred_encoding_keeps_an_explicit_header_charset():
    assert ingest._preferred_encoding("utf-8", "ascii") == "utf-8"


def test_preferred_encoding_falls_back_when_header_missing():
    assert ingest._preferred_encoding(None, "utf-8") == "utf-8"
    assert ingest._preferred_encoding("", "utf-8") == "utf-8"


def test_make_evidence_id_is_length_capped_for_junk_titles():
    # Some pages (e.g. a CBS browser-notice) yield a monster "title"; the id must
    # stay a safe filename (well under the 255-char FS limit) and not crash writes.
    junk = "Notice Your web browser is not fully supported " * 40  # ~1900 chars
    got = ingest.make_evidence_id("2026-06-25", "CBS News Chicago", junk)
    assert got.startswith("2026-06-25-cbs-news-chicago-")  # date + outlet preserved
    assert len(got) <= 120
    assert not got.endswith("-")  # clean slug boundary after truncation


# --- domain + title helpers -------------------------------------------------

def test_domain_of_strips_scheme_and_www():
    assert ingest.domain_of("https://www.wbez.org/housing/2026/x") == "wbez.org"
    assert ingest.domain_of("http://blockclubchicago.org/a/b") == "blockclubchicago.org"


def test_extract_article_returns_text_and_title():
    html = (FIXTURES / "article.html").read_text()
    text, title = ingest.extract_article(html)
    assert "legalize apartment buildings" in text
    assert "apartment legalization" in title.lower()


def test_ingest_article_uses_page_title_when_source_title_missing():
    html = (FIXTURES / "article.html").read_text()
    source = {
        "url": "https://news.example.com/doe",
        "outlet": "Example Chicago News",
        "media_type": "article",
        "title": None,  # not supplied — should fall back to the page's title
        "published_date": "2026-07-06",
    }
    doc = ingest.ingest(source, fetcher=lambda url: html)
    assert "apartment legalization" in doc["title"].lower()


# --- caption normalization --------------------------------------------------

def test_normalize_vtt_strips_cues_and_dedupes_rolling_lines():
    text = ingest.normalize_vtt((FIXTURES / "captions.vtt").read_text())
    # No timestamps or WEBVTT header survive.
    assert "-->" not in text
    assert "WEBVTT" not in text
    # Rolling YouTube-style duplicate lines collapse to one each.
    assert text.count("We should legalize apartments") == 1
    assert text.count("in every neighborhood.") == 1
    assert "That's the whole point." in text


# --- article extraction -----------------------------------------------------

def test_extract_article_text_keeps_body_drops_boilerplate():
    html = (FIXTURES / "article.html").read_text()
    text = ingest.extract_article_text(html)
    assert "legalize apartment buildings in every Chicago neighborhood" in text
    assert "ADVERTISEMENT" not in text
    assert "Subscribe now!" not in text


# --- orchestration with injected fakes --------------------------------------

def test_ingest_article_uses_fetcher_and_returns_transcript_and_meta():
    html = (FIXTURES / "article.html").read_text()
    source = {
        "url": "https://news.example.com/doe-apartments",
        "outlet": "Example Chicago News",
        "media_type": "article",
        "title": "Doe pitches citywide apartment legalization",
        "published_date": "2026-07-06",
    }
    doc = ingest.ingest(source, fetcher=lambda url: html)
    assert "legalize apartment buildings" in doc["transcript"]
    assert doc["id"].startswith("2026-07-06-")
    assert doc["media_type"] == "article"


def test_ingest_audio_downloads_then_transcribes():
    calls = {}

    def fake_downloader(url):
        calls["downloaded"] = url
        return "/tmp/fake-audio.m4a"

    def fake_transcriber(path):
        calls["transcribed"] = path
        return "We should legalize apartments in every neighborhood."

    source = {
        "url": "https://podcast.example.com/ep1.mp3",
        "outlet": "Example Podcast",
        "media_type": "podcast",
        "title": "Doe on housing",
        "published_date": "2026-07-06",
    }
    doc = ingest.ingest(source, downloader=fake_downloader, transcriber=fake_transcriber)
    assert calls["downloaded"] == source["url"]
    assert calls["transcribed"] == "/tmp/fake-audio.m4a"
    assert "legalize apartments" in doc["transcript"]


def test_ingest_youtube_routes_through_audio_download_not_html_fetch():
    # yt-dlp handles YouTube URLs; the old caption path fed page HTML to the
    # caption parser and produced garbage. YouTube must use the audio path.
    calls = {}

    def fake_downloader(url):
        calls["downloaded"] = url
        return "/tmp/yt-audio.m4a"

    def fake_transcriber(path):
        return "We should legalize apartments in every neighborhood."

    def boom_fetcher(url):
        raise AssertionError("YouTube must not use the HTML fetcher")

    source = {
        "url": "https://www.youtube.com/watch?v=abc123",
        "outlet": "WGN News",
        "media_type": "youtube",
        "title": "Candidate on housing",
        "published_date": "2026-07-06",
    }
    doc = ingest.ingest(source, fetcher=boom_fetcher,
                        downloader=fake_downloader, transcriber=fake_transcriber)
    assert calls["downloaded"] == source["url"]
    assert "legalize apartments" in doc["transcript"]
    assert doc["media_type"] == "youtube"  # still recorded accurately
