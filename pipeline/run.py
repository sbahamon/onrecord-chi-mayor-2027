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


def _write_transcript(ingest_doc: dict, data_dir) -> Path:
    path = Path(data_dir) / "transcripts" / f"{ingest_doc['id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {ingest_doc['title']}\n\n"
        f"Source: {ingest_doc['url']}\n"
        f"Outlet: {ingest_doc['outlet']}  ·  {ingest_doc['published_date']}\n\n---\n\n"
    )
    path.write_text(header + ingest_doc["transcript"] + "\n")
    return path


def _write_other(ingest_doc: dict, other_statements: list[dict], data_dir) -> Path:
    path = Path(data_dir) / "positions" / "other" / f"{ingest_doc['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": ingest_doc["id"],
        "url": ingest_doc["url"],
        "outlet": ingest_doc["outlet"],
        "published_date": ingest_doc["published_date"],
        "statements": other_statements,
    }, indent=2) + "\n")
    return path


def process_source(source: dict, *, data_dir, llm, extractor_model: str, today: str,
                   candidates, topics, fetcher=None, downloader=None,
                   transcriber=None) -> ProcessResult:
    ingest_doc = ingest_mod.ingest(
        source, fetcher=fetcher, downloader=downloader, transcriber=transcriber
    )
    extraction = extract_mod.extract(
        ingest_doc["transcript"],
        candidates=candidates,
        topics=topics,
        llm=llm,
        model=extractor_model,
    )

    result = ProcessResult(
        housing_count=len(extraction.housing),
        other_count=len(extraction.other),
    )

    if extraction.other:
        result.other_path = _write_other(ingest_doc, extraction.other, data_dir)

    if not extraction.housing:
        result.pr_body = (
            f"No housing statements found in **{source['title']}** "
            f"({source['outlet']}). Nothing to review."
        )
        return result

    # Persist the transcript so the reviewer can re-verify quotes against it.
    # transcript_ref is the canonical repo-relative path the site/reviewer expect.
    result.transcript_path = _write_transcript(ingest_doc, data_dir)
    ingest_doc["transcript_ref"] = f"data/transcripts/{ingest_doc['id']}.md"

    evidence = propose.build_evidence_record(
        ingest_doc, extraction.housing, discovered_date=today
    )
    stances = propose.propose_stance_updates(evidence, today=today)

    result.evidence_path = propose.write_evidence(evidence, data_dir)
    result.stance_paths = [propose.write_stance(s, data_dir) for s in stances]
    result.pr_body = propose.render_pr_body(evidence, stances)
    return result
