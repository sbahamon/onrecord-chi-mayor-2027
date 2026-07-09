# Chicago '27 Mayoral Housing Tracker

A public-facing tracker of where 2027 Chicago mayoral candidates stand on housing
policy, sourced entirely from their media hits (podcasts, interviews, forums,
articles). New media is discovered proactively on a schedule; every published
position links to the media hit it came from.

**Live site:** https://sbahamon.github.io/onrecord-chi-mayor-2027/

> Working on the code? Read [`CLAUDE.md`](./CLAUDE.md) first — it's the practical
> guide to the architecture, conventions, and the gotchas that only show up in
> real runs.

## How it works

```
daily GitHub Action:  discover -> ingest -> extract positions -> open a PR
on PR:                a cross-family AI reviewer verifies quotes & attribution
you:                  review on GitHub, edit if needed, merge
on merge:             the static site rebuilds and deploys to GitHub Pages
```

- **Discovery** polls Google News RSS, candidate sites, and outlet pages.
- **Ingestion** fetches article text, pulls YouTube captions, or downloads audio
  (podcasts/TikTok/etc.) to a temp runner and transcribes it with Whisper.
  Neither media files nor full transcripts are committed — only extracted quotes
  and a source link. The reviewer re-ingests the source to verify quotes.
- **Extraction** reads the transcript with a cheap near-frontier model
  (DeepSeek via OpenRouter) and proposes structured positions. It captures *all*
  policy topics but only **housing** enters the review queue and the public site.
- **Review** is human-first: an AI reviewer (a *different* model family, e.g.
  Kimi) posts a verdict on each PR as advice; you approve everything for now.
  Auto-publish of high-confidence items is a config switch that ships **off**.

## Repo layout

| Path | What |
|------|------|
| `pipeline/` | Python: discover, ingest, extract, propose, review |
| `schemas/` | JSON Schemas for every data file |
| `data/registry/` | candidates, sources, topics, config (hand-maintained) |
| `data/media-hits/` | evidence records (one per media hit): quotes + source link |
| `data/stances/` | curated matrix cells (candidate × topic) |
| `data/positions/other/` | non-housing captures, unreviewed, unpublished |
| `site/` | Astro static site (the public tracker) |
| `tests/` | pytest suite (runs on fixtures; no network) |

## Data model

Two layers, kept separate on purpose:

- **Evidence** (`data/media-hits/…`) — an immutable record of what was said in a
  media hit, with direct quotes and source links.
- **Stance** (`data/stances/…`) — the curated matrix cell. Its `citations` point
  at specific evidence statements (`"<evidence-id>#<index>"`). The pipeline
  *proposes* stance edits; humans approve them.

## Development

Test-driven. Every pipeline change starts with a failing test.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # backend fixture tests, no network
cd site && npm ci && node --test   # site data-layer smoke tests

# Live tests + real runs need keys in .env (see .env.example):
set -a && . ./.env && set +a
.venv/bin/pytest -m live
```

## Common changes

Most updates are data edits, not code — see [`CLAUDE.md`](./CLAUDE.md) for details.

- **Candidates / topics / models:** edit files in `data/registry/`. Data-integrity
  tests enforce that everything validates and cross-references resolve.
- **Submit a media link by hand:** open an *Add a media hit* issue (the `add-media`
  form) or run the **manual intake** workflow with a URL. Works for TikTok/IG/X too
  (paste the link, or the text if it can't be fetched).
- **What the models look for:** prompts live in `pipeline/extract.py` and
  `pipeline/review.py`.

## Operating notes

- **Secrets** (repo → Settings → Secrets → Actions): `OPENROUTER_API_KEY`,
  `GROQ_API_KEY`, and `PIPELINE_PAT` (a PAT is required so pipeline PRs trigger the
  review workflow — `GITHUB_TOKEN` PRs don't).
- **Human review first:** every position is a PR you approve. Auto-publish is a
  config switch (`data/registry/config.json`) that ships **off**.
- **Nothing copyrighted is stored:** only extracted quotes + a source link; the
  reviewer re-ingests the source to verify.
