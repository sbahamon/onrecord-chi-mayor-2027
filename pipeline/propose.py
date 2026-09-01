"""Assemble extraction output into the files and PR body a human reviews.

Produces, from one media hit:
  * an ``evidence`` record (housing statements, immutable, schema-valid),
  * one proposed ``stance`` cell per (candidate, topic) citing the strongest
    statement,
  * a readable PR body so review is a glance, not a JSON diff.
"""
from __future__ import annotations

from pathlib import Path
import json

from pipeline import schemas


def build_evidence_record(ingest_doc: dict, housing_statements: list[dict],
                          *, discovered_date: str) -> dict:
    for s in housing_statements:
        if not s.get("is_housing"):
            raise ValueError("build_evidence_record only accepts housing statements")
    record = {
        "id": ingest_doc["id"],
        "url": ingest_doc["url"],
        "outlet": ingest_doc["outlet"],
        "media_type": ingest_doc["media_type"],
        "title": ingest_doc["title"],
        "published_date": ingest_doc["published_date"],
        "discovered_date": discovered_date,
        "transcript_ref": ingest_doc.get("transcript_ref"),
        "statements": housing_statements,
    }
    schemas.validate(record, "evidence")
    return record


def propose_stance_updates(evidence: dict, *, today: str) -> list[dict]:
    """One stance per (candidate, topic), citing the highest-confidence statement."""
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    for i, stmt in enumerate(evidence["statements"]):
        key = (stmt["candidate"], stmt["topic"])
        if key not in best or stmt["confidence"] > best[key][1]["confidence"]:
            best[key] = (i, stmt)

    stances = []
    for (candidate, topic), (idx, stmt) in best.items():
        stance = {
            "candidate": candidate,
            "topic": topic,
            "stance": stmt["stance"],
            "summary": stmt["summary"],
            "citations": [f"{evidence['id']}#{idx}"],
            "updated_date": today,
        }
        schemas.validate(stance, "stance")
        stances.append(stance)
    return stances


def render_pr_body(evidence: dict, stances: list[dict]) -> str:
    lines = [
        f"## New media hit: {evidence['title']}",
        "",
        f"- **Source:** [{evidence['outlet']}]({evidence['url']})",
        f"- **Published:** {evidence['published_date']}  ·  **Type:** {evidence['media_type']}",
        f"- **Housing statements extracted:** {len(evidence['statements'])}",
        "",
        "### Proposed stance updates",
        "",
    ]
    stmt_by_key = {(s["candidate"], s["topic"]): s for s in evidence["statements"]}
    for st in stances:
        stmt = stmt_by_key.get((st["candidate"], st["topic"]), {})
        lines += [
            f"#### {st['candidate']} — {st['topic']}: **{st['stance']}**",
            f"{st['summary']}",
            "",
            f"> {stmt.get('quote', '')}",
            f"— {evidence['outlet']}, {evidence['published_date']}"
            + (f" ({stmt['locator']})" if stmt.get("locator") else ""),
            "",
        ]
    lines += [
        "---",
        "_Extracted automatically and pending human review. "
        "Verify each quote against the source before merging._",
    ]
    return "\n".join(lines)


def _safe_join(base: Path, *parts: str) -> Path:
    """Join path parts and refuse anything that escapes ``base``.

    Path segments here derive from untrusted model output (candidate, topic,
    evidence id, date). A crafted value like ``../../ledger`` would otherwise
    let ``write_stance``/``write_evidence`` overwrite arbitrary files under
    ``data/``. Resolve and confirm the result stays inside ``base``.
    """
    base = Path(base).resolve()
    target = base.joinpath(*parts).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"refusing path escaping {base}: {'/'.join(parts)}")
    return target


def write_evidence(evidence: dict, data_dir) -> Path:
    month = evidence["published_date"][:7]  # YYYY-MM
    path = _safe_join(Path(data_dir) / "media-hits", month, f"{evidence['id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    return path


def write_stance(stance: dict, data_dir) -> Path:
    """Write a stance cell, merging with what is already on disk.

    Two fields survive a rewrite; everything else comes from the new proposal.

    ``record`` — what the officeholder actually did, written only by the
    per-candidate backfill. A stance is their *position*; daily discovery
    proposes positions and carries no ``record``, so a wholesale rewrite would
    silently erase the backfilled record the first time discovery touched that
    cell. An explicit ``record`` in ``stance`` still wins, so the backfill can
    edit its own work.

    ``citations`` — unioned, oldest first. ``propose_stance_updates`` runs per
    evidence file and cites only that file's strongest statement, so a position
    backed by several media hits is written one file at a time; replacing would
    leave the cell citing whichever file was processed last and drop the rest
    (#70). Note the consequence: citations only accumulate. A cell whose position
    genuinely changed keeps citing the sources for the old one, and correcting
    that is currently a manual edit.
    """
    path = _safe_join(
        Path(data_dir) / "stances", stance["candidate"], f"{stance['topic']}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    if "record" not in stance and existing.get("record"):
        stance = {**stance, "record": existing["record"]}

    # Union the citations rather than replacing them. `propose_stance_updates`
    # runs per evidence file and cites only that file's best statement, so a cell
    # backed by several media hits is written one file at a time — replacing would
    # leave it citing whichever file was processed last and silently drop the rest
    # (#70). The newest proposal still owns the position itself: stance, summary
    # and updated_date all come from it.
    merged = list(existing.get("citations") or [])
    for c in stance.get("citations") or []:
        if c not in merged:
            merged.append(c)
    if merged:
        stance = {**stance, "citations": merged}

    path.write_text(json.dumps(stance, indent=2) + "\n")
    return path
