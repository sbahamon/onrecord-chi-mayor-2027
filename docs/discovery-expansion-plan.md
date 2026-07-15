# Discovery expansion plan — media + social sources

**Status: COMPLETE (2026-07-09).** All source types shipped and merged to `main` — the daily
`discover` cron now finds articles, **YouTube** (candidate + standing channels), **podcasts**,
and **Bluesky**. **Backfill is done** ([`backfill-plan.md`](./backfill-plan.md)). **Read
`CLAUDE.md` first.** The rest of this doc is the original plan, kept for historical context.

> **Superseded 2026-07-15:** where this plan calls **Google News RSS** the article
> "backbone" and the `website` feed type "not polled," that's now inverted. The backbone
> is **direct outlet RSS** (Block Club / WTTW / Chicago Reader / The TRiiBE / Sun-Times —
> the former `website` feeds, now `type: "rss"`); **Google News is gated off by default**
> (`config.discovery.google_news_enabled: false`) because its links are unreadable
> redirects. See the CLAUDE.md "Non-obvious lessons" entry.

**What shipped** (each TDD + live-verified, merged):
- **Foundation** — browser-UA fetcher + injected headless seam + feed→media-type routing (#22)
- **YouTube** channel feeds — per-candidate + standing (WTTW/WGN/City Club) (#23)
- **Podcasts** — `<enclosure>` audio capture + long-audio transcription downsample (#24)
- **Extraction robustness** — drop individual invalid statements / retry structural failures (#26)
- **Bluesky** — text-only social, candidate-scoped attribution (#27)

**Live verification caught three real bugs offline tests couldn't:** Groq's 413 on long
podcast audio (→ 16 kHz mono ffmpeg downsample), the intake path aborting on an extractor
hiccup (→ retry moved into `process_source` + drop-invalid), and first-person Bluesky posts
mis-attributed (→ scope extraction to the feed's candidate).

**Remaining (tracked follow-ups, not blocking):**
- **Live Playwright headless fetcher** for JS-rendered pages — the injected `headless_fetcher`
  seam exists and is offline-tested; wire a real browser into `cron`/`review`/`intake` CI.
- **YouTube ingestion is bot-gated on CI runner IPs** (#32) — yt-dlp gets `Sign in to confirm
  you're not a bot` from GitHub-runner datacenter IPs (IP-based, hits any length). The YouTube
  path below is "verified" from an un-flagged network; on CI it needs cookies or a proxy. Podcast
  RSS / direct-file audio is unaffected.

**Done:**
- **Chunk very-long (~2 h+) audio** — the downsample covers up to ~106 min at 32 kbps; beyond
  that `transcribe.transcribe_audio` segments the file with ffmpeg (`_split_audio`, duration-probed
  so each piece stays under Groq's ~25 MB cap), transcribes each chunk, and stitches the parts
  (`_stitch_transcripts`). Split/upload are injected seams (`splitter=`/`poster=`) so the chunking
  logic is offline-tested in `tests/test_transcribe.py`. **Live-verified end-to-end with real Groq
  in CI (2026-07-10, #33 closed):** a 2h09m direct-mp3 intake downsampled to 29.7 MB → split into
  2 chunks → both transcribed, no 413.

X / Instagram / TikTok remain **manual-intake only** (no free feed / ToS).

## Why

Today the daily cron only finds **name-matched news articles** (Google News RSS),
and it hardcodes every found item as `media_type: "article"`. So it never ingests
podcasts, candidate-forum videos, YouTube, or social posts on its own — those only
enter via manual intake. This plan teaches *discovery* to pull those automatically.

The audio/video extraction path already works end-to-end (verified: `ingest-url
--type podcast|youtube` → yt-dlp → Groq → extract). The missing piece is **wiring the
cron to find media and route it to the right media_type**, plus adding the feeds.

## Governing constraint: this permanently raises daily review volume

Unlike the backfill (a bounded one-time job), this changes the machine forever. More
sources = more daily PRs, and **audio transcripts are noisier** (no speaker labels,
ASR errors) so expect more `ai-flagged` items. Therefore: **roll out one source type
at a time**, watch the daily PR size and flag rate after each, and keep per-run caps.
Don't enable everything at once.

**Security note:** every source type here feeds *attacker-influenceable* content through
the extractor LLM, so this widens the untrusted-input surface. The `candidate`/`topic`
→ file-path defense (registry-drop in `extract.py` + `^[a-z0-9-]+$` schema pattern +
`propose._safe_join`) already guards the write sink — see CLAUDE.md "Security (LLM output
→ file paths)". Keep those layers intact as you add media/social sources; don't route any
new field into a path or shell without the same treatment.

## Pieces (roughly in rollout order)

### 0. Source research (one-time parallel scout — do first)
Pieces #2–#4 need registry data we don't have yet: each candidate's YouTube
`channel_id` and `bluesky` handle (both are all-`null` today), plus a list of
Chicago-politics podcast RSS feeds and standing channel IDs (WTTW, WGN, City Club).
Gathering these is the one genuinely **breadth-shaped, parallelizable** task in this
plan — independent per-candidate / per-source lookups with no shared state — so it's
the one place a small fan-out helps (a handful of parallel research subagents, *not*
a workflow; the implementation pieces below stay sequential and TDD-gated).

Fan out one research task per candidate (+ a couple for standing channels/podcasts),
each returning a structured row: `slug → {youtube_channel_id, bluesky_handle,
podcast_feeds[]}` with the source URL it found each from. Then **hand-verify every
value before it lands in `candidates.json`/`sources.json`** — subagent output is
untrusted data (see CLAUDE.md's subagent-injection note), and a wrong `channel_id`
silently pulls the wrong person's videos. This is a ~10-minute scouting step, not a
build step; its output is the registry edits that unblock #2–#4.

### 1. Media-type routing in discovery (foundation)
`cmd_discover` currently sets `media_type: "article"` for every item. Change it to
derive the media type from the feed, then route accordingly (the audio path already
works). Two clean options:
- Add a `yields` field to each feed in `sources.json` (e.g. `article`/`youtube`/
  `podcast`), OR
- Map by feed `type`: a `youtube` feed → `youtube`, a new `podcast` feed → `podcast`,
  else `article`.

Prefer the feed-declares-its-media-type approach. Add a `podcast` feed type to
`schemas/sources.schema.json`. TDD the mapping.

### 2. YouTube channel feeds
- Free per-channel RSS: `https://www.youtube.com/feeds/videos.xml?channel_id=<id>`.
- Populate candidate `youtube_channel` in `candidates.json`; extend
  `config.discovery_feeds()` to emit a per-candidate YouTube feed (like it already
  does for Google News). Items route to the `youtube` media_type → audio path.
- Also add a few standing channels (WTTW, WGN, City Club of Chicago forums).

### 3. Podcast feeds
- Add Chicago-politics podcast RSS feeds to `sources.json` (e.g. Ben Joravsky Show).
- **`parse_feed` must capture the audio enclosure URL**, not the episode page — for
  podcast feeds the item `url` should be the enclosure so yt-dlp/Groq get the audio.
  feedparser exposes `entry.enclosures`. TDD with a podcast-RSS fixture.

### 4. Bluesky (social)
- Free public API (`app.bsky.feed.getAuthorFeed`), no auth needed for public posts.
- Populate candidate `bluesky` handles; add a small injected-HTTP Bluesky client
  (`pipeline/bluesky.py`) returning recent posts as items (`media_type: "social"`,
  the post text as the "transcript"). Triage + extract as normal. TDD with a fixture.
- X/Instagram/TikTok stay manual-intake only (API cost / ToS).

### 5. Website-diff for outlet pages (stretch / optional)
`discover.website_changed()` and the `website` feed type exist but aren't polled.
Wiring them means: fetch each `website` source, diff against a stored hash, and when
changed, extract candidate-relevant article links to enqueue. This is closer to
scraping and noisier — do it last, or skip if Google News coverage is enough.

### 6. Fetcher robustness — browser-UA / headless for blocked + JS-rendered pages
Found the hard way during the backfill (Phase 1–2): the default `ingest` fetcher
(`_default_fetcher`, `User-Agent: housing-tracker/0.1` + trafilatura) can't handle
some campaign/outlet sites, and **whatever fix we add must apply to the reviewer too**
— `review.yml` re-ingests the same URL to verify quotes, so if our fetch can't read a
page, the reviewer can't either.
- **403 to non-browser agents** (seen intermittently on `dannicformayor.com`). Cheap
  first step: send a real browser `User-Agent` from `_default_fetcher`.
- **JS-rendered content** where the housing text lives in serialized JSON / client-
  rendered components trafilatura never sees (`dannicformayor.com` platform grid,
  `cardenas4chicago.com/platform.html`). These need a headless render (e.g. Playwright)
  to produce HTML both ingest and the reviewer can read.
Keep the fetcher injected so a headless fetcher can be swapped in and TDD'd offline.
This unblocks the website-diff feature (#5) and any candidate platform page that a
plain fetch can't reach (e.g. danielle-carter-walters, deferred in the backfill for
exactly this reason).

**Sequencing:** treat this as a soft prerequisite, not strictly last — if #0's
research or #2–#4 turn up feeds/pages behind JS-rendering or 403s, pull #6 forward
before them.

## Review-volume controls to add
- Keep `discovery.max_items_per_run`; consider a **per-source-type cap** so one noisy
  podcast feed can't dominate a run.
- A per-feed `enabled` flag already exists — use it to roll out incrementally.
- Watch the first few runs of each new source type before enabling the next.

## Shared code with backfill
The backfill introduces a `backfill` CLI mode and per-candidate PR batching, and will
already exercise the media path for any forum/podcast URLs. This plan reuses the same
media-type routing and `run.process_source`. Doing backfill first means the media
handling is warm before the cron starts leaning on it.

## Verification
- Unit (TDD, offline): feed→media_type mapping, podcast enclosure parsing, Bluesky
  client with injected HTTP, all with fixtures.
- Live: one real run per new source type in a scratch data dir
  (`--data-dir <scratch>`), confirm items route to the right media_type and produce
  sane extractions, before enabling the feed in the cron.
- End-to-end: enable one feed, let the cron open a PR, confirm the reviewer handles the
  media items.

## How to run (in a new session)
1. Confirm the backfill is done/merged first.
2. Say: **"Execute `docs/discovery-expansion-plan.md`, one source type at a time,
   starting with YouTube channels."**
3. It builds each piece TDD, does a live scratch run, enables the feed, and watches the
   first cron PR before moving to the next source type.

Scope decision up front: **which sources, and in what order** (suggested: YouTube →
podcasts → Bluesky → website-diff), and whether to include the optional website-diff.
