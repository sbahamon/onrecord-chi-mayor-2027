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
import tempfile
from pathlib import Path

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"


def download_media(url: str, *, dest_dir: str | None = None) -> str:
    """Download best audio for ``url``, downsample it, and return the local path.

    Whisper only consumes 16 kHz mono, so we re-encode to a compact 16 kHz mono
    low-bitrate MP3 before returning. This is lossless for transcription yet keeps
    long-form audio (podcast episodes, long interviews) under Groq's upload-size
    limit — a ~40-minute episode drops from ~65 MB to ~9 MB. Requires ffmpeg
    (bundled on CI runners). Very long (~2 h+) audio may still exceed the cap;
    chunking is a planned follow-up.
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
                     api_key: str | None = None) -> str:
    """Transcribe a local audio file with Groq Whisper."""
    import requests

    api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

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
