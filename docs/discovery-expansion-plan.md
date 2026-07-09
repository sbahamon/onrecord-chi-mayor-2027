# Discovery expansion plan — media + social sources

**Status:** planned, not started. **Backfill is done** ([`backfill-plan.md`](./backfill-plan.md)) —
this is the next session. **Read `CLAUDE.md` first.**

The article path is already **live**: the scheduled `discover` cron fired 2026-07-09, found
no housing, and opened a ledger-only PR (working as designed). This plan adds the *other*
source types. Note `config.discovery_feeds()` now skips `tracked: false` candidates, so any
per-candidate feeds you add here (YouTube/Bluesky) inherit that drop filter for free.

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

## Pieces (roughly in rollout order)

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
