# Requiring policy specifics: the `mechanism` field

**Status:** approved design, not yet implemented
**Date:** 2026-09-01

## Problem

The tracker records "supports affordable housing" as a position. It isn't one — it is
a greeting. Every candidate is for affordable housing; the accountability question is
*how*.

Today the matrix cannot tell these apart:

| Candidate | Topic | Summary on the site |
|---|---|---|
| brandon-johnson | public-housing-cha | "Will enact a freeze on transferring CHA land to non-housing uses." |
| matthew-brewer | affordable-housing-funding | "Brewer states he is 'all about affordable housing'." |

Both render as `supports`. Of the 25 committed stance cells, roughly a third to a half
are aspiration-only. The incumbent — who has an actual platform — is specific in all
nine of his. **That contrast is the single most useful thing this tracker could tell a
voter, and the data model currently discards it.**

## Decisions taken

1. **Record and mark, do not drop.** A blank cell cannot distinguish "no coverage found"
   from "talks constantly, commits to nothing." Dropping vague statements would erase
   exactly the comparison this change exists to surface.
2. **Capture the mechanism itself**, not a specificity grade. A claim about what the
   transcript says is checkable by the reviewer; a subjective 1–3 grade is not, and
   extraction is already known to vary run to run.
3. **Migrate existing data with a one-time pass over committed quotes**, not by
   re-extracting. Re-extraction is non-deterministic and would churn or silently drop
   human-approved statements.

## Data model

New **optional** field, on both the statement and the stance cell:

```
mechanism: string | null
```

Three states, deliberately distinct:

| State | Meaning | Site treatment |
|---|---|---|
| string | A concrete instrument was named | Show the mechanism |
| `null` | Assessed; the candidate named none | Show "No specific mechanism stated" |
| absent | Not yet assessed | Same as absent today — no marker |

The `absent` state exists only between shipping the field and completing the migration.
It must not render as vague: Johnson's cells are specific, and mislabelling them would
invert the finding.

Optional in both schemas is required, not a preference — both are
`additionalProperties: false`, and ~50 committed statements plus 25 cells would become
schema-invalid (failing CI) if the field were mandatory.

**The stance label does not change.** A candidate who says "I support affordable housing"
does support it; they have not said how. `stance` answers *what*, `mechanism` answers
*how*, and collapsing a vague `supports` into `no-position` would assert something false.

### Definition of a mechanism

A program, rule change, funding source, quantity, or deadline. It must be supported by
the statement's `quote`.

- Mechanism: "waive fees for new affordable housing and fast-track zoning";
  "freeze transfers of CHA land to non-housing uses"; "expand the ADU pilot citywide".
- Not a mechanism: "housing should be affordable"; "build significantly more housing";
  "ensure residents are not displaced"; "an integrated strategy".

Borderline directional phrasing ("streamline permitting") counts **only** if the quote
names what would be streamlined or how. When in doubt the answer is `null` — the failure
we care about is inventing specificity, not missing some.

## Components

### `extract.py` — `SYSTEM_PROMPT`
Add `mechanism` with the definition above and an explicit instruction that it must be
supported by the quote, `null` otherwise. No change to the per-statement schema-drop
path, which still governs what is admitted.

### `review.py` — `REVIEW_SYSTEM`
Add a third judgment alongside faithfulness and attribution: **is the claimed mechanism
actually stated in the transcript?** A statement claiming an unsupported mechanism is
flagged. This is the guard against the extractor inventing specificity to fill the new
field, and it is the reason capturing the mechanism beats grading specificity.

`verify_statement` returns the new judgment; a statement is `confirmed` only if the
mechanism check also passes. Statements with `mechanism: null` skip the check (there is
nothing to verify) and are unaffected.

### `propose.py` — `propose_stance_updates`
Currently picks the highest-confidence statement per (candidate, topic). Change the
ordering to **prefer a statement with a non-null mechanism, then higher confidence**.

This is where most of the user-visible payoff is: without it, a confident-but-vague quote
keeps beating a specific one and the matrix stays vague even after the data improves.

Denormalize the chosen statement's `mechanism` onto the stance cell, exactly as `summary`
and `stance` already are.

### `write_stance`
No change. Citation union/supersede semantics are unaffected.

### Migration script
A one-time script at **`scripts/backfill_mechanism.py`** — deliberately not a `pipeline`
CLI subcommand, because it runs once and must not become part of the standing pipeline
surface (every subcommand is something a future session may invoke by accident):

1. Read every committed evidence file under `data/media-hits/`.
2. For each statement, ask the model what mechanism its `quote` names, if any.
3. Write `mechanism` onto the statement. **Touch nothing else** — no re-fetch, no
   re-extraction, no statement added, removed or reordered (reordering would invalidate
   citation indexes).
4. Recompute each affected stance cell's denormalized `mechanism` from its cited
   statement.

Output is reviewed as a normal PR. ~50 statements, a few cents.

### Site (`site/src/lib/data.js`, `index.astro`, `candidates/[slug].astro`, `methodology.astro`)
- Matrix: a cell whose stance has `mechanism: null` renders muted with a marker, plus a
  legend. The Johnson column should read visibly different from the Giannoulias one.
- Profile: show the mechanism under the stance, or "No specific mechanism stated".
- Methodology: a short section stating the bar and why vague positions are shown rather
  than hidden.

## Testing

Offline, following existing patterns:

- `test_extract.py` — a statement carrying a mechanism survives; the field is optional so
  its absence still validates.
- `test_review.py` — an unsupported mechanism is flagged; `mechanism: null` skips the
  check and can still confirm.
- `test_propose.py` — `propose_stance_updates` prefers a mechanism-bearing statement over
  a higher-confidence vague one; the cell carries the denormalized mechanism.
- `test_data_integrity.py` — existing data without the field still validates.
- Migration script — its per-statement transform is unit-tested with an injected fake LLM;
  no network.
- `site/src/lib/data.test.js` — a null-mechanism cell is distinguishable from an absent one.

## Rollout order

1. Schemas (optional field) — safe alone, nothing else validates differently.
2. Extractor + reviewer + selection rule, with tests.
3. Migration, as its own reviewable PR.
4. Site rendering, once real data exists to look at.

## Known consequence

The matrix will look worse. Several candidates will visibly have nothing concrete on
record. **That is the intent.** Where it is unfair to a candidate the pipeline simply
has not covered much of, the honest remedy is more coverage, not a softer bar — the
methodology section should say so plainly.

## Deliberately out of scope

- Grading *strength* of commitment ("will freeze" vs "will review"). A second axis;
  revisit only if the mechanism field proves insufficient.
- Changing the `stance` enum.
- Re-extracting existing sources.
- Whether backfill should sample a source more than once to counter extraction
  non-determinism. Related, separately open, not blocked by this.
