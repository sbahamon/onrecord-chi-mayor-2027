# Backfill plan — historical data for announced candidates

**Status:** **Backfill effectively complete (2026-07-09), all merged.** `backfill`
mode built; **8 of 11 candidates seeded** — johnson, mendoza, brooks, quigley, holberg,
stanton, brewer, **and george-cardenas** (added 07-09 from his platform housing pillar
`cardenas4chicago.com/pillar-economic-growth.html`, which the Phase-2 pass missed because
the platform page's pillar links are JS-rendered — not the accent). **danielle-carter-walters
was dropped** from the tracker (`tracked: false`; on the methodology "don't track" list).
Still off the matrix: **lisa-nee** (just launched) and **maria-pappas** (no campaign) —
both tracked, empty columns. The outcome tables below are historical; this line is the
current truth. Remaining work is the separate discovery-expansion plan. **Read `CLAUDE.md` first.**

## Why

The daily cron only looks forward (Google News RSS returns recent items). But the
11 candidates declared over months (Oct 2025 – June 2026) and already have campaign
platforms and press coverage from before the tracker existed. The matrix is mostly
empty; a one-time backfill seeds it with their existing positions.

## The governing constraint: review time, not cost or throughput

Extraction is fractions of a cent per item; the pipeline can handle hundreds. The
real limit is that **every position still needs human review** (auto-merge stays off).
So the goal is to maximize signal per item and keep each review batch digestible.
This drives everything below.

## Approach — phased, highest signal first

### Phase 1 — candidate platform / "issues" pages (do this first)
The single best target. A candidate's own site stating their housing plan is
**first-person and directly attributable** — none of the deputy/opponent
misattribution risk that press coverage carries. One high-value page per candidate.

1. Populate each candidate's `website` in `data/registry/candidates.json`, and find
   their housing/issues page URL (usually `/issues`, `/platform`, `/priorities`).
2. Run each through the pipeline (their own words → clean stances).
3. **One PR per candidate** so review is one person at a time.

Expected: ~11 small PRs, matrix fills with defensible positions quickly.

#### Phase 1 outcome (2026-07-08)

> **Note (2026-07-15):** `.github/workflows/backfill.yml` has since been removed (its
> only two recorded runs failed; the data landed via the merged PRs). The `backfill`
> CLI mode remains — future backfills (e.g. #46, Johnson incumbency) are driven from a
> Claude session via `ingest-url` / `backfill` mode. References to the workflow below
> are historical.

Ran via the `backfill` mode + `.github/workflows/backfill.yml`. Not all 11
candidates had a usable platform page, so Phase 1 covered **6**; the rest move to
Phase 2 / later work:

| Candidate | Phase 1 | Source used / why deferred |
|-----------|---------|----------------------------|
| brandon-johnson | ✅ PR | dedicated `/issues/afforable-housing` page (richest) |
| joe-holberg | ✅ PR | dedicated `/priorities` page |
| susana-mendoza | ✅ PR | `/priorities` ("Build, Build, Build") |
| toni-brooks | ✅ PR | `/platforms` (tax-relief framing) |
| mike-quigley | ✅ PR | homepage line only (thin) |
| liam-stanton | ✅ PR | homepage plank; **hand-extracted** (see below) |
| matthew-brewer | ⏭ Phase 2 | live site, no housing/issues page |
| george-cardenas | ⏭ Phase 2 | mayoral site's platform page states no housing |
| lisa-nee | ⏭ Phase 2 | bio-only site, no platform page |
| maria-pappas | ⏭ later | no campaign site yet (launch deferred) |
| danielle-carter-walters | ⏭ blocked | site 403s the fetcher — **needs browser UA in `ingest`** |

`website` is now populated for 10/11 candidates (all but Pappas).

**Two lessons from the live run:**
- **Manual-extraction fallback.** The auto-extractor persistently failed on
  Stanton's busy multi-plank homepage (emitted a schema-invalid empty-quote
  statement; `extract.py` rightly rejects it). Fix: fetch the page through the same
  `ingest`, take a **verbatim** quote from that text, and feed it to
  `run.process_source` via a hand-authored statements payload (a fake `llm`). The
  `quote_in_transcript` guard still runs and `review.yml` still re-verifies — so the
  record is as trustworthy as a model-extracted one. Never write a quote from memory.
- **Fetcher-blocked sites.** Carter-Walters' site returns 403 to the trafilatura
  fetcher. Getting her (and similar) platform pages needs a browser user-agent /
  headless render in `ingest.py` — tracked as a discovery-expansion item, not a
  Phase 2 press item.

### Phase 2 — curated key press per candidate
For each candidate, the 3–5 most substantive housing interviews/articles (find via
dated web searches). Batch **one PR per candidate**. Adds specifics the platform
boilerplate lacks. Expect more `ai-flagged` items here (press attribution is noisier)
— that's the reviewer doing its job.

**Ready to run — no new code.** Build a rows file (same shape as
`data/backfill/phase1.json`) with `type: "article"` entries and run the same
`backfill` mode / `backfill.yml` workflow (its `slugs` input can target a subset).
Priorities for Phase 2:
- **The 4 candidates Phase 1 couldn't seed from a platform page** —
  matthew-brewer, george-cardenas, lisa-nee, and (once a browser-UA fetch exists)
  danielle-carter-walters. These have *no* matrix data yet, so press is their only
  route in.
- Then depth for the 6 already seeded, where a platform page was thin (esp.
  mike-quigley, whose only Phase 1 signal was a single homepage line).
- maria-pappas stays out until she launches a campaign (press coverage of a
  non-candidate isn't a stated position).

### Phase 3 — optional comprehensiveness
A one-time date-ranged news sweep only if Phases 1–2 leave gaps. Noisiest, highest
review burden, lowest signal per item. The daily cron already covers everything new.

## The `backfill` CLI mode — **built (2026-07-08)**

`ingest-url` is one-URL→one-PR; the cron is one big daily PR. Backfill is the middle
mode. Implemented in `pipeline/backfill.py` (`run_backfill`) + `cmd_backfill` in
`pipeline/__main__.py`, driven by `.github/workflows/backfill.yml`. `python -m pipeline backfill`:

- **Input:** a JSON/CSV list of `{candidate_slug, url, type, outlet?, date?}` rows
  (or, for Phase 1, just read each candidate's `website`/issues page).
- **Groups output into one PR per candidate** (not one mega-PR) — the key design point.
  Reuse `run.process_source`; group by candidate; emit a per-candidate PR body.
- **Bypasses `max_items_per_run`** (that cap is for the daily trickle).
- **Adds every processed URL to the ledger** (`data/ledger.json`) so the daily cron
  never re-processes a backfilled item. Backfill and cron then coexist cleanly.
- Build it TDD like everything else (fixtures + injected llm/fetcher). It's mostly
  orchestration over already-tested units (`ingest`, `extract`, `propose`).

**As built, two behaviors beyond the original sketch (both from the live run):**
- **Per-row retry + loud failure.** `extract.py` deliberately *raises* on a
  schema-invalid statement (a trust decision — a model not following the contract
  isn't half-trusted). Models occasionally emit one bad field on an otherwise-good
  page, so `run_backfill` retries each row (`max_attempts=3`); a row that never
  succeeds is recorded and its URL left un-marked in the ledger (so it can be re-run),
  and `cmd_backfill` exits non-zero so the workflow job fails visibly instead of
  opening an empty PR.
- **PR fan-out is a workflow matrix, not Python.** PR creation lives in Actions.
  `backfill.yml` runs one isolated `backfill --only <slug>` job per candidate (so
  `add-paths: data` stages only that candidate's files), opens the PR via
  `PIPELINE_PAT` (so `review.yml` fires), and a final `seed-ledger` job records all
  URLs once — keeping the per-candidate PRs free of `ledger.json` merge conflicts. A
  `slugs` dispatch input re-runs a subset.

## Related gaps worth folding in (optional, decide per scope)

- **Ongoing media + social discovery is a separate, sequenced plan** —
  [`discovery-expansion-plan.md`](./discovery-expansion-plan.md), to run **after** this
  backfill. It reuses the same media handling. Keep it out of scope here (this is a
  bounded one-time job; that one changes the cron permanently).
- **Video/audio extraction works** (verified live). `ingest-url --type
  podcast|youtube` → yt-dlp → Groq → extract. So the backfill *can* include forum
  videos / podcast episodes; just expect noisier transcripts (more reviewer flags).

## How to run (in a new session)

1. Open a session in this repo. Say something like:
   > "Execute the backfill in `docs/backfill-plan.md`, Phase 1 only" (or "Phases 1–2").
2. It should: read this plan + `CLAUDE.md`, populate `website`/issues URLs (asking you
   to confirm the URLs it finds), build the `backfill` mode TDD, run Phase 1 into
   per-candidate PRs, and hand you the PRs to review.
3. You review/merge per candidate. Then optionally Phase 2.

Scope decision to make up front: **Phase 1 only, or Phases 1–2**, and how exhaustive
the press dig should be.
