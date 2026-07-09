# CLAUDE.md — working notes for future instances

Guidance for making changes to this repo. Read this before editing.

## What this is

A public accountability tracker of where 2027 Chicago mayoral candidates stand on
housing, sourced only from their media appearances. A scheduled pipeline discovers
media, extracts positions with an LLM, and opens a PR; a second LLM (different family)
verifies each quote; a human approves before anything publishes. Static site on
GitHub Pages.

- **Live site:** https://sbahamon.github.io/onrecord-chi-mayor-2027/
- **Repo:** https://github.com/sbahamon/onrecord-chi-mayor-2027 (public)

## Golden rules

1. **TDD, always.** Every pipeline change starts with a failing test. Watch it fail,
   then implement. The suite runs offline on fixtures — no network, no keys.
   `.venv/bin/pytest` (72+ tests). Live tests are `-m live` (need keys).
2. **Human review before publish.** `auto_merge_enabled` in `data/registry/config.json`
   ships `false`. There is a test (`test_review.py`) asserting auto-merge stays off
   regardless of verdicts. Do not casually flip this — it's a trust decision for the user.
3. **Never invent facts about real people.** Don't add candidates, quotes, or stances
   from memory. Candidates come from a cited source; quotes come from transcripts and are
   verified to appear in them (`extract.py` drops any quote not found in the transcript).
4. **Never commit media files or full transcripts.** Only extracted quotes + a source
   link are stored (copyright). `.gitignore` blocks media and `data/transcripts/`.
   The reviewer re-ingests the source to verify (`review.review_evidence`).

## Architecture / data flow

```
discover  -> ingest        -> extract         -> propose            -> PR (via PAT)
(feeds)      (transcript)     (statements)       (evidence+stances)    |
                                                                       v
                                              review (re-ingest + verify) posts verdict + label
                                                                       |
                                              human approves/merges  -> site rebuilds -> Pages
```

Everything external is **dependency-injected** so it's testable offline:
`ingest(source, fetcher=, downloader=, transcriber=)`, `extract(..., llm=)`,
`review_evidence(..., ingest_fn=)`. The CLI (`pipeline/__main__.py`) wires the real
implementations; tests pass fakes.

## Module map (`pipeline/`)

| File | Responsibility |
|------|----------------|
| `schemas.py` | Load JSON Schemas (`schemas/*.schema.json`), `validate(record, name)` |
| `data_integrity.py` | Walk `data/`, map each file to its schema |
| `citations.py` | Resolve `"<evidence-id>#<index>"` → statement |
| `discover.py` | RSS parse, `Ledger` dedup, website-diff, LLM triage |
| `ingest.py` | Article text (trafilatura), captions, audio→transcript; `domain_of`, title extraction |
| `transcribe.py` | yt-dlp download + Groq Whisper (the only heavy external step) |
| `llm.py` | `OpenRouterLLM.complete_json` — OpenAI-compatible, injectable `post`, retries |
| `extract.py` | LLM → validated statements; **enforces quote-in-transcript**, housing/other routing |
| `propose.py` | Build evidence record + stance cells + PR body; write files |
| `review.py` | Deterministic quote check + model judgment; label + auto-merge gate |
| `config.py` | Load registries; `candidate_slugs`, `topic_slugs`, `discovery_feeds` |
| `run.py` | `process_source`: orchestrates ingest→extract→propose for one source |
| `__main__.py` | CLI: `ingest-url`, `discover`, `review` |

## Data model (two layers)

- **Evidence** (`data/media-hits/YYYY-MM/<id>.json`) — immutable record of one media hit:
  outlet, url, date, and housing `statements` (each with a verbatim `quote`,
  `attribution_flag`, `confidence`). `transcript_ref` is always `null` (not stored).
- **Stance** (`data/stances/<candidate>/<topic>.json`) — the curated matrix cell:
  a `stance` label + `summary` + `citations` (`["<evidence-id>#<index>"]`). The pipeline
  *proposes* these; humans approve. Non-housing captures go to `data/positions/other/`
  (unreviewed, unpublished).

Stance enum: `supports | supports-with-conditions | opposes | mixed | no-position`.

## Registries (`data/registry/`, hand-edited)

- `candidates.json` — slug, name, status (`incumbent|declared|rumored|withdrawn|example`),
  optional website/bluesky/youtube_channel, and a per-name `google_news_rss`.
- `topics.json` — the matrix rows (housing taxonomy).
- `sources.json` — shared discovery feeds (Google News, outlet pages).
- `config.json` — model ids, `auto_merge_enabled`, discovery caps.

**Data integrity is enforced by tests:** every file under `data/` validates against its
schema, every stance references a known candidate+topic, and every citation resolves.
Break any of these and CI fails — that's intentional (a bad merge can't corrupt the site).

## Models (via OpenRouter, `config.json > models`)

- extractor + triage: `deepseek/deepseek-v3.2`
- reviewer: `moonshotai/kimi-k2-0905` (deliberately a *different family* than the extractor)

**Gotcha:** OpenRouter model slugs are exact and change. `deepseek/deepseek-chat-v3.2`
and plain `moonshotai/kimi-k2` do NOT work here (the latter lacks JSON-mode). Verify a slug
before changing: `curl https://openrouter.ai/api/v1/models` or test a `response_format:
{type: json_object}` call. To change models, edit `config.json` only — no code change.

## Common changes (how-to)

- **Add/remove a candidate:** edit `data/registry/candidates.json`. Give a lowercase-kebab
  `slug`, a `status`, and a `google_news_rss` (pattern: `https://news.google.com/rss/search?q=<url-encoded "Name" Chicago mayor>&hl=en-US&gl=US&ceid=US:en`). `discovery_feeds()` picks it up automatically. Active-only excludes `example`/`withdrawn`.
- **Add a housing topic (matrix row):** add to `data/registry/topics.json` (unique slug, `order`). The matrix and profiles pick it up on rebuild.
- **Change a model or discovery cap:** `data/registry/config.json`.
- **Change what the extractor/reviewer looks for:** the prompts are `SYSTEM_PROMPT` in
  `extract.py` and `REVIEW_SYSTEM` in `review.py`. Add a test if behavior changes.
- **Turn on auto-publish (user decision):** set `auto_merge_enabled: true` and
  `auto_merge_min_confidence`; then wire the review workflow to merge on `ai-verified`.
  Update the `should_auto_merge` test to match.

## Site (`site/`, Astro → Pages)

- Build-time data layer: `site/src/lib/data.js` reads `../../../data` and builds the
  matrix/profiles/feed. Smoke-tested with `node --test` (`data.test.js`).
- Pages: `index.astro` (matrix), `candidates/[slug].astro`, `feed.astro`, `methodology.astro`.
- **Gotcha (base path):** GitHub `configure-pages` gives a base path with NO trailing slash.
  `astro.config.mjs` normalizes it to end in `/` so `import.meta.env.BASE_URL + "feed"`
  joins correctly. Always prefix internal links with `import.meta.env.BASE_URL`. A CI check
  in `test.yml` fails if links lose the slash.

## Workflows & secrets

- `test.yml` — pytest + site build/link check on every PR (gates data PRs too).
- `deploy.yml` — build + deploy to Pages on push to `main`.
- `cron.yml` — daily `discover` → PR.
- `intake.yml` — manual URL (issue form `add-media` or workflow_dispatch) → PR.
- `review.yml` — on pipeline PRs: re-ingest + verify → comment + `ai-verified`/`ai-flagged` label.

Secrets: `OPENROUTER_API_KEY`, `GROQ_API_KEY`, and `PIPELINE_PAT` (a PAT is required so
pipeline PRs *trigger* the review workflow — `GITHUB_TOKEN`-created PRs don't fire workflows).

**Security:** intake consumes untrusted issue input — it enters only as `env:` vars, is
parsed/sanitized in a fixed Python heredoc, then passed as quoted shell vars. Keep that
pattern for any new workflow that reads issue/PR/comment text.

**`create-pull-request` gotcha:** use `add-paths: data` (a whole dir). Listing globs like
`data/positions/**` fails the git add when a run produces no such subdir, losing the commit.

## Verifying changes end-to-end

- Offline: `.venv/bin/pytest` and `cd site && node --test`.
- Live (needs keys in `.env`): `set -a && . ./.env && set +a && .venv/bin/pytest -m live`.
- Real run without touching the repo: copy `data/registry` into a scratch dir and
  `python -m pipeline --data-dir <scratch> ingest-url --url <real article>`; inspect the
  written evidence/stances, then `... review <evidence.json>`.
- The live loop: trigger `intake` workflow → a PR opens → `review` workflow comments on it.

## Non-obvious lessons (paid for in real runs)

- Only live runs catch: wrong model slugs, Pages base-path link breakage, `add-paths`
  glob-miss, ugly URL-slug IDs. After nontrivial changes, do a real run, not just tests.
- The extractor is a bit loose on attribution (it will tag a deputy's or opponent's words
  to the candidate). The reviewer catches this from the quote text — that's the whole point
  of the two-model, human-approved design. Don't "fix" it by trusting the extractor more.
