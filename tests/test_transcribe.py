"""Unit tests for pipeline.transcribe — the download/downsample/transcribe seam.

Groq's transcription endpoint caps upload size (~25 MB), so audio longer than the
16 kHz-mono downsample covers (~106 min at 32 kbps) must be segmented and stitched
rather than 413ing. These tests exercise the chunking *decision* offline by
injecting fake split/upload seams; the real ffmpeg and Groq calls stay behind them.
"""
import os
import subprocess

import pytest

from pipeline import transcribe


def _write_file(path, size):
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return str(path)


def test_small_file_uploads_once_without_splitting(tmp_path):
    audio = _write_file(tmp_path / "ep.16k.mp3", 1024)
    calls = {"posted": [], "split": 0}

    def fake_poster(path, *, model, api_key):
        calls["posted"].append(path)
        return "a full short transcript"

    def fake_splitter(path):
        calls["split"] += 1
        raise AssertionError("must not split a file under the cap")

    text = transcribe.transcribe_audio(
        audio, api_key="k", poster=fake_poster, splitter=fake_splitter
    )
    assert calls["posted"] == [audio]
    assert calls["split"] == 0
    assert text == "a full short transcript"


def test_oversized_file_is_split_transcribed_and_stitched(tmp_path):
    audio = _write_file(
        tmp_path / "long.16k.mp3", transcribe.GROQ_MAX_UPLOAD_BYTES + 1
    )
    chunk_a = str(tmp_path / "long.part000.mp3")
    chunk_b = str(tmp_path / "long.part001.mp3")
    posted = []

    def fake_splitter(path):
        assert path == audio
        return [chunk_a, chunk_b]

    def fake_poster(path, *, model, api_key):
        posted.append(path)
        return {chunk_a: "first half ends", chunk_b: "second half begins"}[path]

    text = transcribe.transcribe_audio(
        audio, api_key="k", poster=fake_poster, splitter=fake_splitter
    )
    # Every chunk uploaded, in order, and the transcripts concatenated.
    assert posted == [chunk_a, chunk_b]
    assert text == "first half ends second half begins"


def test_transcribe_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    audio = _write_file(tmp_path / "ep.16k.mp3", 10)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        transcribe.transcribe_audio(audio)


def test_stitch_transcripts_joins_parts_with_single_space():
    # Whisper transcribes each segment independently; stitching trims per-chunk
    # whitespace and joins with one space so a word split across a boundary at
    # worst becomes two words rather than a run-on.
    assert (
        transcribe._stitch_transcripts(["  first part.  ", "", "second part.  "])
        == "first part. second part."
    )


def test_chunk_target_stays_under_the_hard_cap():
    # Segments must land comfortably below the upload limit, not right at it.
    assert transcribe.CHUNK_TARGET_BYTES < transcribe.GROQ_MAX_UPLOAD_BYTES


def test_chunks_are_deleted_after_stitching(tmp_path, monkeypatch):
    """Segments are scratch. Left behind they accumulate per long-audio row.

    On an ephemeral CI runner that is invisible; run locally — which is now the
    path for outlets that block datacenter IPs — it fills the maintainer's disk.
    """
    monkeypatch.setattr(transcribe, "GROQ_MAX_UPLOAD_BYTES", 512)
    audio = _write_file(tmp_path / "ep.16k.mp3", 4096)
    chunks = [_write_file(tmp_path / f"ep.part{i:03d}.mp3", 256) for i in range(3)]

    transcribe.transcribe_audio(
        audio, api_key="k",
        splitter=lambda p: chunks,
        poster=lambda p, *, model, api_key: "part",
    )

    for c in chunks:
        assert not os.path.exists(c), f"chunk left on disk: {c}"


def test_chunks_are_deleted_even_when_an_upload_fails(tmp_path, monkeypatch):
    """A failed row must not be a row that also leaks its scratch files."""
    monkeypatch.setattr(transcribe, "GROQ_MAX_UPLOAD_BYTES", 512)
    audio = _write_file(tmp_path / "ep.16k.mp3", 4096)
    chunks = [_write_file(tmp_path / f"ep.part{i:03d}.mp3", 256) for i in range(2)]

    def boom(path, *, model, api_key):
        raise RuntimeError("groq 500")

    with pytest.raises(RuntimeError):
        transcribe.transcribe_audio(audio, api_key="k",
                                    splitter=lambda p: chunks, poster=boom)

    for c in chunks:
        assert not os.path.exists(c), f"chunk left on disk after failure: {c}"


def test_downsample_removes_the_original_download(tmp_path, monkeypatch):
    """The yt-dlp original is the biggest artifact and is dead once re-encoded."""
    raw = _write_file(tmp_path / "ep.m4a", 2048)

    def fake_run(cmd, **kw):
        # Stand in for ffmpeg: produce the output file it would have written.
        _write_file(cmd[-1], 128)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(transcribe.subprocess, "run", fake_run)
    out = transcribe._downsample_for_whisper(raw)

    assert os.path.exists(out)
    assert not os.path.exists(raw), "original download left on disk"
