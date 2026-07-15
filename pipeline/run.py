"""Orchestration: take a media source all the way to reviewable files + PR body.

    source -> ingest (transcript) -> extract (statements)
           -> evidence + stance files (housing)  [published after review]
           -> other-capture file (non-housing)   [unreviewed, unpublished]

Dependencies (fetcher/downloader/transcriber/llm) are injected so the whole flow
is testable offline; the CLI wires the real ones.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import extract as extract_mod
from pipeline import ingest as ingest_mod
from pipeline import propose


@dataclass
class ProcessResult:
    evidence_path: Path | None = None
    stance_paths: list = field(default_factory=list)
    other_path: Path | None = None
    transcript_path: Path | None = None
    pr_body: str = ""
    housing_count: int = 0
    other_count: int = 0
    transcript_chars: int = 0  # length only (text is never stored) — for discovery logs


def _write_other(ingest_doc: dict, other_statements: list[dict], data_dir) -> Path:
    path = propose._safe_join(
        Path(data_dir) / "positions" / "other", f"{ingest_doc['id']}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": ingest_doc["id"],
        "url": ingest_doc["url"],
        "outlet": ingest_doc["outlet"],
        "published_date": ingest_doc["published_date"],
        "statements": other_statements,
    }, indent=2) + "\n")
    return path


def _extract_with_retry(transcript, *, candidates, topics, llm, model, attempts):
    """Extract statements, retrying on failure.

    ``extract`` deliberately raises on a lone schema-invalid statement (e.g. the
    model occasionally emits an empty ``quote``); a retry with a fresh model call
    usually recovers it. Retrying here — not by re-running ``process_source`` —
    reuses the transcript, so audio sources are not re-downloaded/re-transcribed.
    """
    last_error = None
    for _ in range(max(1, attempts)):
        try:
            return extract_mod.extract(
                transcript, candidates=candidates, topics=topics, llm=llm, model=model
            )
        except Exception as e:  # noqa: BLE001 — transient bad field / model error; retry
            last_error = e
    raise last_error


def process_source(source: dict, *, data_dir, llm, extractor_model: str, today: str,
                   candidates, topics, fetcher=None, downloader=None,
                   transcriber=None, extract_attempts: int = 3) -> ProcessResult:
    ingest_doc = ingest_mod.ingest(
        source, fetcher=fetcher, downloader=downloader, transcriber=transcriber
    )
    extraction = _extract_with_retry(
        ingest_doc["transcript"],
        candidates=candidates,
        topics=topics,
        llm=llm,
        model=extractor_model,
        attempts=extract_attempts,
    )

    result = ProcessResult(
        housing_count=len(extraction.housing),
        other_count=len(extraction.other),
        transcript_chars=len(ingest_doc["transcript"]),
    )

    if extraction.other:
        result.other_path = _write_other(ingest_doc, extraction.other, data_dir)

    if not extraction.housing:
        result.pr_body = (
            f"No housing statements found in **{source['title']}** "
            f"({source['outlet']}). Nothing to review."
        )
        return result

    # Transcripts are not stored (copyright); the reviewer re-ingests the source
    # to verify quotes. Only the extracted quotes + source link are published.
    ingest_doc["transcript_ref"] = None

    evidence = propose.build_evidence_record(
        ingest_doc, extraction.housing, discovered_date=today
    )
    stances = propose.propose_stance_updates(evidence, today=today)

    result.evidence_path = propose.write_evidence(evidence, data_dir)
    result.stance_paths = [propose.write_stance(s, data_dir) for s in stances]
    result.pr_body = propose.render_pr_body(evidence, stances)
    return result
