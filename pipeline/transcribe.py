"""Download media (yt-dlp) and transcribe it (Groq Whisper).

These are the pieces that touch external tools and the Groq API. They are kept
tiny and dependency-injected at the ``ingest`` seam so the rest of the pipeline
stays testable. Downloaded media is written to a caller-provided temp dir and is
never committed; callers are responsible for cleaning it up (the GitHub runner
discards its filesystem after each job).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"

# Groq's transcription endpoint rejects uploads over ~25 MB with 413. The
# downsample keeps most episodes well under this, but a long forum/interview
# (~2 h+) still exceeds it, so oversized audio is segmented before upload.
GROQ_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Aim each segment comfortably below the hard cap: the headroom absorbs
# per-segment bitrate variance and multipart-upload overhead.
CHUNK_TARGET_BYTES = 18 * 1024 * 1024


def download_media(url: str, *, dest_dir: str | None = None) -> str:
    """Download best audio for ``url``, downsample it, and return the local path.

    Whisper only consumes 16 kHz mono, so we re-encode to a compact 16 kHz mono
    low-bitrate MP3 before returning. This is lossless for transcription yet keeps
    long-form audio (podcast episodes, long interviews) under Groq's upload-size
    limit — a ~40-minute episode drops from ~65 MB to ~9 MB. Requires ffmpeg
    (bundled on CI runners). Very long (~2 h+) audio still exceeds the cap even
    downsampled; ``transcribe_audio`` segments those before upload.
    """
    import yt_dlp

    dest_dir = dest_dir or tempfile.mkdtemp(prefix="httrack-")
    outtmpl = str(Path(dest_dir) / "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        raw_path = ydl.prepare_filename(info)

    return _downsample_for_whisper(raw_path)


def _downsample_for_whisper(raw_path: str) -> str:
    """Re-encode audio to 16 kHz mono ~32 kbps MP3 (Whisper-friendly, compact)."""
    compact_path = str(Path(raw_path).with_suffix(".16k.mp3"))
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", raw_path,
         "-ac", "1", "-ar", "16000", "-b:a", "32k", compact_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg downsample failed (exit {result.returncode}): "
            f"{result.stderr[-500:]}"
        )
    return compact_path


def transcribe_audio(path: str, *, model: str = "whisper-large-v3-turbo",
                     api_key: str | None = None,
                     splitter=None, poster=None) -> str:
    """Transcribe a local audio file with Groq Whisper.

    A file within Groq's upload cap goes up in one request. A larger one
    (very long audio the downsample can't shrink under the cap) is split into
    time segments, each transcribed, and the parts stitched back together — so a
    2 h+ source transcribes fully instead of 413ing.

    ``splitter`` (path -> list of chunk paths) and ``poster`` (chunk -> text) are
    injection seams: the defaults shell out to ffmpeg/Groq, tests pass fakes so
    the chunking decision stays offline-testable.
    """
    api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

    poster = poster or _post_transcription
    size = os.path.getsize(path)
    if size <= GROQ_MAX_UPLOAD_BYTES:
        return poster(path, model=model, api_key=api_key)

    splitter = splitter or _split_audio
    chunks = splitter(path)
    # Rare path (very long audio) — log it so a live run shows chunking engaged.
    print(
        f"transcribe: audio {size / 1_048_576:.1f} MB over "
        f"{GROQ_MAX_UPLOAD_BYTES / 1_048_576:.0f} MB cap; "
        f"split into {len(chunks)} chunk(s)",
        file=sys.stderr,
    )
    parts = [poster(chunk, model=model, api_key=api_key) for chunk in chunks]
    return _stitch_transcripts(parts)


def _post_transcription(path: str, *, model: str, api_key: str) -> str:
    """Upload one audio file to Groq Whisper and return its transcript text."""
    import requests

    with open(path, "rb") as fh:
        resp = requests.post(
            GROQ_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(path).name, fh)},
            data={"model": model, "response_format": "text"},
            timeout=600,
        )
    resp.raise_for_status()
    return resp.text.strip()


def _stitch_transcripts(parts: list[str]) -> str:
    """Join per-segment transcripts with a single space.

    Time-based segmentation can cut mid-word; a lone space at the seam keeps that
    at worst a two-word split rather than a run-on, and drops empty parts.
    """
    return " ".join(p.strip() for p in parts if p and p.strip())


def _split_audio(path: str, *, target_bytes: int = CHUNK_TARGET_BYTES) -> list[str]:
    """Segment ``path`` into files each roughly ``target_bytes`` in size.

    The segment duration is derived from the file's actual bytes-per-second
    (probed, so it holds whatever the real bitrate is) so each piece lands under
    the upload cap. Uses ffmpeg's stream-copy segmenter — no re-encode.
    """
    duration = _probe_duration(path)
    size = os.path.getsize(path)
    bytes_per_second = (size / duration) if duration else size
    segment_seconds = max(1, int(target_bytes / bytes_per_second))

    stem = Path(path).with_suffix("")
    seg_template = f"{stem}.part%03d.mp3"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-f", "segment",
         "-segment_time", str(segment_seconds), "-c", "copy", seg_template],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg segment failed (exit {result.returncode}): "
            f"{result.stderr[-500:]}"
        )
    chunks = sorted(str(p) for p in Path(path).parent.glob(f"{stem.name}.part*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg segment produced no chunks")
    return chunks


def _probe_duration(path: str) -> float:
    """Return the media duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (exit {result.returncode}): {result.stderr[-500:]}"
        )
    return float(result.stdout.strip())
