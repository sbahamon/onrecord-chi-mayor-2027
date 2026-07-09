# Backfill plan — historical data for announced candidates

**Status:** planned, not started. Execute in a fresh session (see "How to run").
**Read `CLAUDE.md` first.**

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

### Phase 2 — curated key press per candidate
For each candidate, the 3–5 most substantive housing interviews/articles (find via
dated web searches). Batch **one PR per candidate**. Adds specifics the platform
boilerplate lacks. Expect more `ai-flagged` items here (press attribution is noisier)
— that's the reviewer doing its job.

### Phase 3 — optional comprehensiveness
A one-time date-ranged news sweep only if Phases 1–2 leave gaps. Noisiest, highest
review burden, lowest signal per item. The daily cron already covers everything new.

## Code to add: a `backfill` CLI mode

`ingest-url` is one-URL→one-PR; the cron is one big daily PR. Backfill needs a middle
mode. Add `python -m pipeline backfill`:

- **Input:** a JSON/CSV list of `{candidate_slug, url, type, outlet?, date?}` rows
  (or, for Phase 1, just read each candidate's `website`/issues page).
- **Groups output into one PR per candidate** (not one mega-PR) — the key design point.
  Reuse `run.process_source`; group by candidate; emit a per-candidate PR body.
- **Bypasses `max_items_per_run`** (that cap is for the daily trickle).
- **Adds every processed URL to the ledger** (`data/ledger.json`) so the daily cron
  never re-processes a backfilled item. Backfill and cron then coexist cleanly.
- Build it TDD like everything else (fixtures + injected llm/fetcher). It's mostly
  orchestration over already-tested units (`ingest`, `extract`, `propose`).

Prep alongside the code:
- Populate `website` fields in `candidates.json`.
- For Phase 1, collect the 11 issues-page URLs (web search each candidate).

## Related gaps worth folding in (optional, decide per scope)

- **Social media is not checked.** No candidate has a `bluesky`/`youtube_channel`
  handle set; no Bluesky feed is wired into discovery. If wanted, populate handles and
  add a Bluesky source (its public API is free and easy) — but that's *ongoing
  discovery*, not backfill, so keep it a separate task.
- **Video/audio extraction is built but unverified end-to-end** (see CLAUDE.md). If the
  backfill includes any YouTube forums or podcast episodes, verify that path on one real
  URL first (`ingest-url --type youtube|podcast`) before batching.

## How to run (in a new session)

1. Open a session in this repo. Say something like:
   > "Execute the backfill in `docs/backfill-plan.md`, Phase 1 only" (or "Phases 1–2").
2. It should: read this plan + `CLAUDE.md`, populate `website`/issues URLs (asking you
   to confirm the URLs it finds), build the `backfill` mode TDD, run Phase 1 into
   per-candidate PRs, and hand you the PRs to review.
3. You review/merge per candidate. Then optionally Phase 2.

Scope decision to make up front: **Phase 1 only, or Phases 1–2**, and how exhaustive
the press dig should be.
