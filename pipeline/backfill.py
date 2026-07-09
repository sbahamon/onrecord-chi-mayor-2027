"""Backfill: process a bounded list of URLs into one reviewable bucket per candidate.

The middle mode between ``ingest-url`` (one URL -> one PR) and ``discover`` (one big
daily PR). It exists to seed the matrix from historical sources — most importantly
each candidate's own platform/issues page — while keeping review digestible: output
is grouped **per candidate** so the GitHub Actions layer can open one PR each.

Each row is scoped to a single candidate (``candidates=[slug]``) on purpose: a
candidate's own page is first-person, so the extractor must not be able to attribute
its words to anyone else. Reuses ``run.process_source`` unchanged; the only new logic
is grouping, body-joining, and marking the ledger so the daily cron never re-processes
a backfilled URL.

Dependencies (llm/fetcher/downloader/transcriber/ledger) are injected so the whole
flow is testable offline; the CLI wires the real ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pipeline import ingest as ingest_mod
from pipeline import run

_BODY_DIVIDER = "\n\n---\n\n"


@dataclass
class CandidateBackfill:
    candidate_slug: str
    results: list = field(default_factory=list)   # one ProcessResult per row
    pr_body: str = ""                             # combined, one per candidate
    paths: list = field(default_factory=list)     # evidence + stance Paths to stage
    housing_count: int = 0


def _source_from_row(row: dict, *, today: str) -> dict:
    url = row["url"]
    return {
        "url": url,
        "outlet": row.get("outlet") or ingest_mod.domain_of(url),
        "media_type": row.get("type", "website"),
        "title": row.get("title"),  # None -> ingest fills from the page for HTML
        "published_date": row.get("date") or today,
    }


def run_backfill(rows, *, data_dir, llm, extractor_model: str, today: str, topics,
                 ledger=None, fetcher=None, downloader=None,
                 transcriber=None) -> list[CandidateBackfill]:
    """Process ``rows`` grouped into one :class:`CandidateBackfill` per candidate.

    ``rows`` are dicts ``{candidate_slug, url, type?, outlet?, date?, title?}``.
    First-seen candidate order is preserved. No ``max_items`` cap applies — the
    daily trickle limit is for the cron, not a one-time backfill.
    """
    buckets: dict[str, CandidateBackfill] = {}

    for row in rows:
        slug = row["candidate_slug"]
        bucket = buckets.get(slug)
        if bucket is None:
            bucket = buckets[slug] = CandidateBackfill(candidate_slug=slug)

        result = run.process_source(
            _source_from_row(row, today=today),
            data_dir=data_dir,
            llm=llm,
            extractor_model=extractor_model,
            today=today,
            candidates=[slug],   # scope: a candidate's page speaks only for them
            topics=topics,
            fetcher=fetcher,
            downloader=downloader,
            transcriber=transcriber,
        )
        bucket.results.append(result)
        bucket.housing_count += result.housing_count

        for p in [result.evidence_path, *result.stance_paths]:
            if p is not None and p not in bucket.paths:
                bucket.paths.append(p)

        if ledger is not None:
            ledger.mark(row["url"])

    if ledger is not None:
        ledger.save()

    for bucket in buckets.values():
        bodies = [r.pr_body for r in bucket.results if r.pr_body]
        bucket.pr_body = _BODY_DIVIDER.join(bodies)

    return list(buckets.values())
