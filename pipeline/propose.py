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


def _rank(stmt: dict) -> tuple[int, float]:
    """Prefer a statement that names a mechanism, then higher confidence.

    Without the first term a confident platitude ("we need more housing", 0.95)
    outranks a concrete proposal ("end apartment bans", 0.70) and the cell stays
    vague even when specific evidence sits in the same source. The extractor is
    reliably confident about platitudes, so confidence alone selects for them.
    """
    return (1 if stmt.get("mechanism") else 0, stmt.get("confidence", 0.0))


def propose_stance_updates(evidence: dict, *, today: str) -> list[dict]:
    """One stance per (candidate, topic), citing the most specific statement.

    "Most specific" is a named mechanism first, confidence second — see ``_rank``.
    """
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    for i, stmt in enumerate(evidence["statements"]):
        key = (stmt["candidate"], stmt["topic"])
        if key not in best or _rank(stmt) > _rank(best[key][1]):
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
        # Denormalized like `summary`, and only when the statement was assessed.
        # Copying a key that isn't there would turn "not yet assessed" into
        # "vague" and mark every un-migrated cell wrongly.
        if "mechanism" in stmt:
            stance["mechanism"] = stmt["mechanism"]
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

    Some fields survive a rewrite; everything else comes from the new proposal.

    ``record`` — what the officeholder actually did, written only by the
    per-candidate backfill. A stance is their *position*; daily discovery
    proposes positions and carries no ``record``, so a wholesale rewrite would
    silently erase the backfilled record the first time discovery touched that
    cell. An explicit ``record`` in ``stance`` still wins, so the backfill can
    edit its own work.

    ``citations`` — unioned **across** evidence files, oldest first, but a new
    citation **supersedes** any existing one from the same evidence id.
    ``propose_stance_updates`` runs per evidence file and cites only that file's
    strongest statement, so a position backed by several media hits is written one
    file at a time; replacing outright would leave the cell citing whichever file
    was processed last and drop the rest (#70).

    Superseding within a source is not an optimisation — it is required. A
    citation pins a statement *index*, and extraction is not reproducible run to
    run (observed live: one article yielded 0, 2 and 3 statements on separate runs
    at temperature 0). Accumulating indexes from the same file would leave
    ``hit#2`` behind after a re-run that produced two statements, and a dangling
    citation fails the data-integrity tests.

    Consequence worth knowing: citations from *different* sources only accumulate.
    A cell whose position genuinely changed keeps citing the sources for the old
    one, and correcting that is a manual edit.

    ``stance`` + ``summary`` + ``mechanism`` — normally the newest proposal owns
    the position, but a proposal naming **no** mechanism cannot overwrite a cell
    that names one (#90). The three move as a unit because they describe a single
    statement. See the comment on the guard below for why order alone made this
    unsafe.
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

    # A proposal that names no mechanism must not overwrite one that does (#90).
    # `stance`/`summary`/`mechanism` used to come from whichever evidence file was
    # written last, and processing order is arbitrary — alphabetical inside a
    # backfill, discovery order in the cron. So a weak source silently replaced a
    # strong one: seen live, a launch-day article replaced "supports a millionaire's
    # tax" + a named 3% rate with an off-topic school-funding line and no mechanism,
    # while still citing the statement that named the rate. Nothing failed — schemas
    # passed and the citation resolved — the cell just stopped matching its evidence.
    #
    # The three fields move together because they describe one statement: keeping a
    # summary while taking a new stance would caption support with "opposes". The
    # guard is deliberately one-way — an incoming mechanism still wins, so better
    # sourcing can always improve a cell — and it does not rank two named mechanisms
    # against each other, which is a judgment this code can't make; that case stays
    # last-write-wins. An absent `mechanism` key counts as no mechanism: it means
    # "not yet assessed", which is even weaker ground to overwrite from than null.
    if existing.get("mechanism") and not stance.get("mechanism"):
        stance = {**stance,
                  **{k: existing[k] for k in ("stance", "summary", "mechanism")
                     if k in existing}}

    # Union the citations rather than replacing them. `propose_stance_updates`
    # runs per evidence file and cites only that file's best statement, so a cell
    # backed by several media hits is written one file at a time — replacing would
    # leave it citing whichever file was processed last and silently drop the rest
    # (#70). The newest proposal still owns the position itself: stance, summary
    # and updated_date all come from it.
    incoming = list(stance.get("citations") or [])
    superseded = {c.partition("#")[0] for c in incoming}
    merged = [c for c in (existing.get("citations") or [])
              if c.partition("#")[0] not in superseded]
    merged += [c for c in incoming if c not in merged]
    if merged:
        stance = {**stance, "citations": merged}

    path.write_text(json.dumps(stance, indent=2) + "\n")
    return path
