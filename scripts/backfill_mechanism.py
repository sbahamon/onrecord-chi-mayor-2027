"""One-time migration: add `mechanism` to already-committed statements.

Reads committed evidence, asks the model what mechanism each quote names, and
writes the field. It ONLY adds a field — no re-fetch, no re-extraction, no
statement added, removed or reordered.

That restraint is the whole design. Re-extracting would re-fetch (several outlets
intermittently block datacenter ranges) and is not reproducible: the same article
has yielded 0, 2 and 3 statements on separate runs at temperature 0. It could
therefore drop human-approved statements, or reorder them — and a citation pins a
statement *index*, so reordering silently repoints every citation in the repo.

Deliberately a script rather than a `pipeline` CLI subcommand: it runs once, and
every subcommand is something a future session may invoke by accident.

Usage:
    set -a && . ./.env && set +a
    .venv/bin/python -m scripts.backfill_mechanism data
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pipeline import config, schemas

SYSTEM = (
    "You are given a quote from a candidate and the summary drawn from it. "
    "Identify the concrete policy mechanism the QUOTE names: a program, a rule "
    "change, a funding source, a quantity, or a deadline. Respond as JSON: "
    '{"mechanism": "<short phrase>"} or {"mechanism": null}. '
    "Use null when the quote expresses only a goal, a value, or a direction "
    "without saying how — 'housing should be affordable', 'build more housing', "
    "'ensure residents aren't displaced' are all null. Directional phrasing "
    "counts ONLY if the quote names what would change: 'streamline permitting' "
    "is null, 'cut permit review to 30 days' is a mechanism. When in doubt use "
    "null; never invent specificity the quote does not contain."
)


def assess_mechanism(quote: str, summary: str, *, llm, model: str) -> str | None:
    answer = llm.complete_json(
        model=model, system=SYSTEM,
        user=f"Summary: {summary}\nQuote: {quote}",
    )
    mechanism = answer.get("mechanism")
    return mechanism if isinstance(mechanism, str) and mechanism.strip() else None


def migrate(data_dir, *, llm, model: str) -> dict:
    data_dir = Path(data_dir)
    n_statements = 0

    for path in sorted((data_dir / "media-hits").rglob("*.json")):
        evidence = json.loads(path.read_text())
        changed = False
        for stmt in evidence["statements"]:      # mutated in place: order preserved
            if "mechanism" in stmt:
                continue                          # already assessed; re-running is free
            stmt["mechanism"] = assess_mechanism(
                stmt["quote"], stmt["summary"], llm=llm, model=model)
            n_statements += 1
            changed = True
        if changed:
            schemas.validate(evidence, "evidence")
            path.write_text(json.dumps(evidence, indent=2) + "\n")

    return {"statements": n_statements, "stances": _refresh_stances(data_dir)}


def _refresh_stances(data_dir: Path) -> int:
    """Copy each cell's cited statement's mechanism onto the cell.

    Writes the file directly rather than going through ``propose.write_stance``,
    which would union citations and rewrite ``updated_date``. This migration
    changes exactly one field.
    """
    by_id = {}
    for path in sorted((data_dir / "media-hits").rglob("*.json")):
        ev = json.loads(path.read_text())
        by_id[ev["id"]] = ev

    stance_dir = data_dir / "stances"
    if not stance_dir.exists():
        return 0

    n = 0
    for path in sorted(stance_dir.rglob("*.json")):
        stance = json.loads(path.read_text())
        # The last citation is the most recently proposed, so its statement is
        # the one whose stance and summary this cell currently carries.
        ev_id, _, idx = stance["citations"][-1].rpartition("#")
        ev = by_id.get(ev_id)
        if not ev or not idx.isdigit() or int(idx) >= len(ev["statements"]):
            continue                              # dangling: skip, never crash
        stmt = ev["statements"][int(idx)]
        if "mechanism" not in stmt:
            continue
        # Compare only when the cell HAS the key. `stance.get("mechanism")` on a
        # cell without one returns None, which equals a null mechanism — so a
        # plain equality check skips exactly the vague cells this field exists to
        # mark, leaving them indistinguishable from un-migrated ones.
        if "mechanism" in stance and stance["mechanism"] == stmt["mechanism"]:
            continue
        stance["mechanism"] = stmt["mechanism"]
        schemas.validate(stance, "stance")
        path.write_text(json.dumps(stance, indent=2) + "\n")
        n += 1
    return n


def main(argv: list[str]) -> int:
    from pipeline.llm import OpenRouterLLM

    data_dir = Path(argv[1] if len(argv) > 1 else "data")
    cfg = config.load_config(data_dir)
    counts = migrate(data_dir, llm=OpenRouterLLM(), model=cfg["models"]["extractor"])
    print(f"statements={counts['statements']} stances={counts['stances']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
