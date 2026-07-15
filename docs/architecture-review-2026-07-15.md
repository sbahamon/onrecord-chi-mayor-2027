# Architecture review — 2026-07-15

A full audit of the project (every code file, all issues, PR history, Actions run
history, and the data layer), done to answer the owner's question: *is this project
simultaneously overengineered (goal: cheap, fully cloud-hosted — "wouldn't a scheduled
Claude call be simpler?") and underengineered (the ideal ingestion mechanisms are
blocked because they run from GitHub Actions cron)?*

This doc records the findings, the decisions made from them, and the sequenced next
steps. **Future Claude sessions: read this before working on any of the linked issues.**

---

## TL;DR

The pipeline's *logic* is sound and its trust core is the product. It is built on the
wrong *runtime* for its hardest job: fetching the open web from GitHub-runner datacenter
IPs. One root cause — runner IP reputation — explains every ingestion failure (#30, #32,
#41, and the Google News redirect failure fixed in #42). The automated discovery cron
had merged **zero** statements as of this review; everything on the site came from the
one-time backfill plus one manual intake. Decisions: prune a little dead code, do NOT
replace the pipeline with a general "scheduled Claude call", add a **length-capped
Gemini transcription path** for short YouTube clips (eval first), and use scheduled
Claude sessions for the jobs Claude is actually better at: discovery beyond fixed feeds,
and curated historical backfills.

---

## Verified findings (as of 2026-07-15, pre-#42 daily runs)

**Scale.** ~1,770 LOC pipeline (15 modules), ~1,900 LOC tests (127 tests, offline by
design), ~510 LOC Astro site, 6 workflows (428 LOC YAML), 7 JSON schemas. External
moving parts: OpenRouter (DeepSeek triage/extract + Kimi review), Groq Whisper, yt-dlp,
ffmpeg, trafilatura, feedparser, Bluesky AppView, GitHub Actions/Pages, a PAT.

**Yield.** Automated daily discovery: **0 statements ever merged.** All 42 statements /
24 stance cells (~27% of the 10-tracked-candidate × 9-topic grid) came from the two-day
backfill (2026-07-08/09) + one hand-fed WBEZ article. The 7 daily cron runs to date all
ran green while producing ledger-only PRs ("No new housing statements found") — the
silent-empty failure mode fixed by #42. Ledger: 119 seen URLs, 113 of them dead Google
News redirects.

**Per-source status.**

| Source | Status in CI | Why |
|---|---|---|
| Direct outlet RSS (Block Club, WTTW, Reader, TRiiBE, Sun-Times) | working since #42, degraded | article-page 429s on runner IPs (#41); 429'd items retry next run |
| Podcasts (Joravsky, Spielman, City Cast) | functional | verified live once; zero production yield so far |
| Bluesky (per-candidate) | functional | supplied-text path, no fetch gate; zero yield so far |
| YouTube (City Club + per-candidate) | **dead** | yt-dlp bot-gate on runner IPs (#32), IP-based, any length |
| Google News | gated off | redirect links unreadable without a headless fetcher (#30); fixed by moving to direct RSS (#42) |

**Where the complexity lives.** Roughly 25–35% of pipeline LOC is scaffolding for the
batch/CI environment (ledger mark-after-success retry semantics, global/per-feed caps,
audio chunking for Groq's 25 MB cap, reviewer re-ingesting because runner FS is
ephemeral, PAT so bot PRs trigger workflows, injection/path-traversal guards). The core
discover→ingest→extract→propose→review flow is comparatively small and linear.

**Cost today.** Effectively $0/month: free Actions + Pages on a public repo, sub-cent
DeepSeek calls, Groq ≈ $0.04/hr of audio only when a podcast passes triage.

**Content reality check.** The first working post-#42 RSS run triaged 120 items down to
**one** housing hit. Nine months out, candidates generate little on-record housing
content; a perfect ingestion layer probably yields 1–3 statements/week, each still
needing human review. The 27%-full grid reflects reality more than pipeline failure.
Don't over-invest in ingestion breadth.

---

## Decision log

1. **NO-GO on replacing the pipeline with a general "scheduled Claude call".** It
   doesn't fix the root cause (an agent sandbox has its own egress limits; yt-dlp still
   can't download YouTube), it costs more than ~$0, and it would forfeit the
   reproducible verbatim-quote verification that lets a public site make claims about
   real politicians. The trust core (quote-in-transcript check, two-model review,
   schemas, data-integrity tests, human merge gate) stays exactly as is.
2. **Claude sessions ARE the right tool for two jobs** the deterministic pipeline is bad
   at: (a) *discovery beyond fixed feeds* (web search finds TV hits and one-off
   interviews no polled feed carries, and notices when yield is zero — the silent
   Google-News week would have been caught on day one), and (b) *curated historical
   backfills* where the hard part is source curation, not execution. In both cases the
   session feeds URLs into the existing intake path — Claude curates, the pipeline
   extracts and verifies. Never quotes from memory (golden rule #3).
3. **YouTube: length-capped Gemini path instead of #32 cookies/proxy.** The Gemini eval
   ([gemini-transcription-eval-log.md](./gemini-transcription-eval-log.md)) was NO-GO
   for long-form (non-reproducible, runaway) but positive for short clips (≤10:39
   proven clean and cheap). Cookies rot and proxies cost/get gated; short news hits are
   where on-record statements mostly happen anyway. Forums/debates stay untranscribable
   in CI until/unless #32 is solved — an accepted tradeoff.
4. **Eval before build.** The 11–20 min band is untested (the eval jumped 10:39 → 49
   min), and the fuzzy-matcher threshold needs the *Gemini-vs-Gemini* drift
   distribution, which was never measured (the eval only measured Gemini-vs-Groq,
   0.80–0.94 — the wrong comparison for the reviewer's re-ingest check).
5. **Prune (done in this review's PR):** `ingest.normalize_vtt` + `captions.vtt`
   fixture (caption path never wired), `pyyaml` (never imported),
   `.github/workflows/backfill.yml` (0-for-2 run history; superseded by
   Claude-session-driven backfills via the kept `pipeline/backfill.py` CLI). Kept:
   `website_changed`, `citations.py`/`data_integrity.py` (test guardrails), the headless
   seam, Google News registry data, and `evals/gemini_transcription/` (the harness the
   calibration eval extends).

---

## Sequenced next steps (each has a GitHub issue — check it before starting)

### 1. Eval: calibrate Gemini short-clip transcription
Extend `evals/gemini_transcription/run_eval.py` with 3–4 real Chicago-relevant clips in
the **11–20 min band** + 1 proven short clip, 2 runs each, `gemini-2.5-flash` native SDK,
`media_resolution=LOW`, temp 0. **Gemini-only — no Groq/yt-dlp baseline needed**, because
the calibration questions are Gemini-vs-Gemini. Key enabling fact (verified,
`run_eval.py::gemini_transcribe_native`): the native SDK passes the YouTube URL as
`file_uri`, so **Google fetches the video server-side** — no YouTube egress from the
runner, #32 does not apply, and the eval can run in CI via a small `workflow_dispatch`
workflow (owner is phone-only; results go to the run summary/artifact, findings appended
to the eval log). Outputs: (a) an evidence-based duration cap (15 min if the band
passes, else 11–12); (b) the run1-quotes-vs-run2-transcript fuzzy-ratio distribution →
the matcher threshold. Prereq: `GEMINI_API_KEY` repo secret. Cost ≈ $1.

### 2. Implement the Gemini short-clip YouTube path (blocked by step 1)
Design (details matter — all were paid for in the eval):
- **Duration gate:** YouTube Data API v3 `videos.list?part=contentDetails` (free key,
  1 unit/call) — **not yt-dlp metadata, which is bot-gated on runner IPs too.** Injected
  `http_get` seam; parse ISO-8601 duration. Config knob
  `transcription.gemini_youtube_max_minutes` (value from the eval). New secret
  `YOUTUBE_API_KEY`.
- **Transcriber:** port `gemini_transcribe_native` from the eval harness into the
  pipeline (injected client seam; retry on 524/400/read-timeout). Model
  `gemini-2.5-flash` — **not** flash-lite (sunset for new API keys). `google-genai` goes
  in `[live]` extras.
- **Routing (`ingest.py` audio path):** YouTube URL → duration lookup → ≤cap ⇒ Gemini;
  >cap ⇒ raise a clear "long YouTube unsupported in CI (#32)" error so the item stays
  un-ledgered (retries; effectively skipped until #32).
- **Strict fuzzy quote matcher (trust-critical):** Gemini isn't byte-reproducible even
  on short clips at temp 0, and the reviewer re-transcribes on re-ingest, so
  exact-substring would false-flag real quotes. Add a normalized sliding-window
  similarity fallback *after* the exact check, in both `extract.py` and
  `review.verify_statement`, threshold from the eval (~0.90 expected). Adversarial
  near-miss tests required: fabricated quote, right-topic-wrong-words, mis-attributed
  speaker must all still fail. The per-statement schema check (path-injection guard)
  stays untouched.
- **Separation of powers:** Gemini transcribes ONLY. DeepSeek extracts, Kimi reviews.
  Never let Gemini both produce and judge a quote.
- **Workflows:** `GEMINI_API_KEY` + `YOUTUBE_API_KEY` env in `cron.yml`, `review.yml`,
  `intake.yml`; install `google-genai` where `[live]` installs.
- Cost: cents per clip (Flash native low-res ≈ $0.22/hr; review re-ingest doubles it).

### 3. Weekly scheduled-Claude discovery Routine
A weekly Claude session that web-searches each tracked candidate for housing coverage
the RSS feeds missed, dedups against `data/ledger.json` + `data/media-hits/`, confirms
pages are readable, and feeds survivor URLs into the existing intake path (`intake.yml`
dispatch or `ingest-url`). Extraction/verification/human review unchanged. Doubles as a
zero-yield health alarm. ~1 session/week.

### 4. Backfill Brandon Johnson's incumbency record (2023–2027), Claude-session driven
A session researches and curates real sources from his term (interviews, budget
addresses, press coverage), then drives the existing CLI (`ingest-url` per source, or
`backfill` mode with a phase file). Every quote still passes the verbatim-in-transcript
guard; `review.yml` + human merge still gate. Scope question to resolve first: stated
*positions* fit the existing evidence→stance model as-is (just older dates); his
*record* (successes/failures, outcomes) does not fit the stance schema and needs its own
design decision.

### Meanwhile / deferred
- **Week-one RSS validation — tracked in #47** (full checklist + decision tree there;
  a one-shot scheduled Claude session fires 2026-07-22 15:00 UTC to execute it and
  comments its verdict on the issue). #41 (429s) is handled by retry-next-run and only
  matters if block-club/reader throughput does.
- **Headless fetcher (#30)** stays deferred; the weekly Claude session can hand-fetch
  the occasional JS-shell page more cheaply than wiring Playwright into 3 workflows.
- **`auto_merge_enabled` stays `false`** — human review is the bottleneck by design.
