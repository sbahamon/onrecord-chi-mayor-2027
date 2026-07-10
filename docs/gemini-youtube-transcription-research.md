# Gemini URL-based YouTube transcription — feasibility research (issue #36)

> **⚠️ RESOLVED 2026-07-10 — see [`gemini-transcription-eval-log.md`](./gemini-transcription-eval-log.md).**
> The conditional GO/NO-GO below was settled by a live eval (Phase 1 OpenRouter + Phase 2 native
> SDK low-res). **Verdict: NO-GO as a general path** — Gemini's long-form transcription is
> non-reproducible/runaway (the 88-min forum gave 54k vs 290k chars on identical runs; low-res
> cleared only the token-ceiling, not the instability). **Recommendation: pursue #32 (yt-dlp
> cookies/proxy).** Gemini remains a viable **short-clip (≤~11 min) fallback** if #32 fails. The
> doc below is the pre-eval feasibility analysis, kept for context.

**Status: RESEARCH ONLY — no production code changed (2026-07-10).** Deliverable is this
findings doc + a go/no-go. **Read `CLAUDE.md` first** (transcription notes + the #32 YouTube
bot-gate lesson). Answers issue [#36](https://github.com/sbahamon/onrecord-chi-mayor-2027/issues/36);
related: [#32](https://github.com/sbahamon/onrecord-chi-mayor-2027/issues/32) (YouTube bot-gate,
open), #33 + PR #34 (long-audio chunking, done).

**Bottom line — CONDITIONAL GO to *de-risk*, NO-GO on committing yet.** The core idea is
sound: passing a YouTube URL to Gemini moves the video fetch **server-side to Google**, which
*should* bypass the GitHub-runner IP bot-gate (#32), and — importantly — **OpenRouter already
passes YouTube URLs through to Gemini**, so it can reuse `OPENROUTER_API_KEY` with **no new
secret**. Cost is small-to-nil (~$0–20/mo; **Gemini 2.5 Flash-Lite ≈ Groq-parity**, confirmed
against Google's pricing page). **But** two documented problems hit *this project*
specifically and must be cleared by a live spike before adoption: (1) Gemini's YouTube fetch is
**flaky for very recent videos** (<~1 month old) — and this tracker ingests fresh media; and (2)
Gemini's transcript **varies run-to-run and may not be strictly verbatim** (unlike Groq-Whisper,
a reproducible temp-0 dedicated ASR), which breaks the exact-substring `quote_in_transcript`
re-verification that is the spine of the project's trust model. Recommendation: run the CI spike
in §D, and treat the simpler **yt-dlp cookies/proxy fix in #32 as the lower-risk default** unless
the spike clears both risks.

---

## Why

Today's audio path is `yt-dlp download → ffmpeg 16 kHz-mono downsample → Groq Whisper`
(`pipeline/transcribe.py`; `pipeline/ingest.py` `AUDIO_TYPES`). It works for **podcasts /
direct-file audio**, but **YouTube is bot-gated on CI runner IPs** (#32): yt-dlp gets `Sign in to
confirm you're not a bot` from GitHub-Actions datacenter IPs. It's IP-based (any length fails) and
degrades the real `cron`/`review` YouTube discovery + verification paths. The hypothesis: let
**Gemini** ingest a public YouTube URL directly (Google fetches the video), removing yt-dlp from
the YouTube path.

## Governing constraint (the trust model — do not weaken)

The project's credibility rests on three things this research must preserve, not just note:

1. **Independent, deterministic re-verification.** The reviewer re-ingests the source and checks
   each quote appears verbatim: `verify_statement` AND-gates `quote_in_transcript` (in
   `pipeline/extract.py` — whitespace-collapsed, case-folded **substring containment**) with a
   different-family reviewer LLM (`pipeline/review.py`). The model *cannot* override a missing
   quote. This works today because **Groq Whisper is reproducible in practice** — it's a
   dedicated ASR run at temperature-0 (greedy decoding), so the same audio file yields ~the same
   transcript on re-ingest and the exact substring check holds. (Not *perfectly* bit-identical —
   GPU float nondeterminism + Whisper's temperature-fallback cause a small drift, which is why
   `CLAUDE.md` already notes audio flags more than articles — but stable enough that the exact
   match survives re-ingest today.)
2. **Different-family reviewer.** Extractor is DeepSeek, reviewer is Kimi *on purpose*
   (`data/registry/config.json > models`). A second family checks the first.
3. **Human review before publish** (`auto_merge_enabled: false`, asserted by `test_review.py`);
   **never invent facts** — quotes come from the source and are verified to appear in it.

An LLM transcription step collides head-on with #1 (see §Trust-model handling).

---

## A. Feasibility — does it work, especially in CI/CD?

### It works, via a documented mechanism

- Gemini accepts a **YouTube URL directly** as a `fileData` part with the URL in `file_uri`
  (mime `video/*`) — no download, no File API upload for YouTube specifically. Google's servers
  retrieve the video. Supported on **Gemini 2.5 Flash / Pro** (and later). Sources:
  [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/video-understanding),
  [File input methods | Gemini API](https://ai.google.dev/gemini-api/docs/file-input-methods).
- **Limits (primary docs):** **public videos only** (not private/unlisted); **1 video per
  request** recommended (max 10 for 2.5+); **free tier ≤ 8 h of YouTube/day**, **paid tier no
  length cap**; video sampled at **1 FPS**; clip windows via `video_metadata`
  `start_offset`/`end_offset`. Source:
  [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/video-understanding).
- **Transcription quality:** Gemini can transcribe and can emit timestamps on request, but it is
  a general model, **not a dedicated ASR** — output is not guaranteed strictly verbatim (it may
  normalize filler words / disfluencies). No speaker diarization guarantee. This matters for a
  *verbatim-quote* project (see Trust-model handling).

### CI/CD crux — the fetch is server-side (the whole point), with caveats

The YouTube fetch happens **on Google's infrastructure**, not the client, so a flagged
GitHub-runner IP never touches YouTube — this is the mechanism that would bypass #32. OpenRouter's
own docs confirm the URL approach "allows the provider to retrieve the video from its source."
**However, "Google fetches it" is not "Google fetches it reliably":**

- **Recent-video flakiness (biggest risk for us).** Multiple current reports: **videos less than
  ~1 month old fail to load correctly** (wrong titles, timeouts) via the API. This tracker
  ingests *fresh* candidate media, so this is not an edge case — it's the common case. Sources:
  [Recent YouTube Videos Inaccessible via Gemini API](https://discuss.ai.google.dev/t/recent-youtube-videos-inaccessible-via-gemini-3-flash-preview-api/114076),
  [Certain YouTube links don't work](https://discuss.ai.google.dev/t/certain-youtube-links-dont-work/88350).
- **Intermittent transient 400s.** YouTube-URL input returns intermittent `400 INVALID_ARGUMENT`
  that succeeds on retry (mis-typed as a 400 rather than a retryable 503). Manageable with
  retries, but real. Source:
  [Intermittent 400 on YouTube URL input](https://discuss.ai.google.dev/t/youtube-url-video-input-returns-intermittent-400-invalid-argument-should-be-503-or-retryable-error-code/125883).

**Verdict on the crux:** the approach *should* clear the #32 IP gate because the fetch is
Google-side, and there is no documented GitHub-runner-specific gate on the Gemini API itself — but
the **recent-video failures could reintroduce "YouTube silently doesn't work" through a different
door.** This cannot be settled from docs; it needs the live CI spike in §D against a *fresh*
public video. Treat any "it works in CI" claim as unproven until then.

### Native vs OpenRouter

- **OpenRouter passes YouTube URLs through to Gemini.** Video input is supported via the
  `video_url` content part on `/api/v1/chat/completions`; the docs explicitly state that for
  **Google AI Studio** Gemini you must pass a **YouTube link** (Vertex does not support it).
  Source: [OpenRouter — Video Inputs](https://openrouter.ai/docs/guides/overview/multimodal/videos).
  → We can **reuse `OPENROUTER_API_KEY`** (`pipeline/llm.py` `OpenRouterLLM`) and pin the provider
  to `google-ai-studio`. **No new secret.** (Native `GEMINI_API_KEY` + SDK is the fallback if
  OpenRouter's passthrough proves unreliable.)
- Caveat: our current `OpenRouterLLM.complete_json` builds **text-only** messages. Video needs a
  multimodal `content` array (a `video_url` part). That's a small new method, not a rewrite.

---

## B. Re-architecture

The transcription layer is already dependency-injected (map below), so a Gemini path can hide
behind the same seams and the offline suite stays fixture-only (golden rule #1, TDD).

| Seam (innermost → outermost) | Where | Change |
|---|---|---|
| `transcribe_audio(path, *, model, poster=, splitter=)` | `pipeline/transcribe.py` | For YouTube, **not used** — the Gemini path takes a URL, not a downsampled file. No ffmpeg/chunking on this path. |
| `download_media(url)` | `pipeline/transcribe.py` | For YouTube, **replaced** by "pass the URL straight to Gemini." yt-dlp/ffmpeg drop off the YouTube path (still needed for podcasts). |
| `ingest(source, *, downloader=, transcriber=)` | `pipeline/ingest.py:160-165` | The audio branch composes `transcriber(downloader(url))`. Add a **Gemini transcriber** injected as `transcriber=` when `media_type == "youtube"`; keep Groq for `podcast`/`social`/`manual`. |
| `review_evidence(evidence, *, ingest_fn)` | `pipeline/review.py` | Reviewer re-runs `ingest` (rebuilt source has **no `text`**, so it re-transcribes). The Gemini path must satisfy this too — this is where non-determinism bites (§Trust). |
| `OpenRouterLLM(..., post=)` / new multimodal call | `pipeline/llm.py` | Add a `complete_video`/multimodal path (a `video_url` content part, `provider: google-ai-studio`). Note: **base URL is hardcoded** (`ENDPOINT`); the native-Gemini fallback would need a new base URL/SDK. |
| Routing | `pipeline/__main__.py` | `AUDIO_TYPES`/`media_type_for_feed` already isolate `youtube`; route it to the Gemini transcriber. |
| Config | `data/registry/config.json` | Add a transcription model id. **Note:** a `transcription.model` key already exists but is **not threaded through** any call site (`transcribe_audio` uses its own default) — wire it through as part of this. |
| Workflows | `.github/workflows/{cron,review,intake}.yml` | The YouTube path no longer needs ffmpeg/yt-dlp (podcasts still do, so keep them). No new secret if OpenRouter route. |

**Recommended scope — hybrid, not a full swap.** Keep **Groq for podcasts / direct-file audio**
(works today, deterministic, ~$0.04/hr). Use **Gemini only for YouTube** (the broken path). A full
unification onto Gemini would trade a working, deterministic, cheap path for a non-deterministic,
pricier one for no benefit.

**Two *independent* scope decisions — keep them separate:**

1. **Which media types use Gemini** → **YouTube only.** Everything else stays on the existing
   Groq/yt-dlp path *unchanged* (it works; no "simplified" rewrite is warranted — the ffmpeg
   downsample + long-audio chunking only exist to feed Groq, which podcasts still need). The
   YouTube path is the only one that *loses* ffmpeg/yt-dlp, because Gemini takes the URL directly.

2. **How deep into the pipeline Gemini goes → transcription ONLY, not analysis (recommended).**
   Gemini returns a plain transcript string that flows into the **unchanged** DeepSeek extractor
   (`extract.py`) and different-family Kimi reviewer (`review.py`). **Gemini never selects quotes
   or judges faithfulness/attribution.** This is precisely why it hides cleanly behind the
   `transcriber=` seam, and it keeps trust rule #2 (different-family checker) fully intact — the
   transcription model and the extraction/review models are different families by construction.

   The alternative — **Gemini also extracts/analyzes** (one call transcribes *and* picks the
   housing quotes/stances) — is **not recommended**: it would collapse transcription and analysis
   into a single model, putting the same model on both sides of the extract→review check and
   gutting the independent-checker design. It's also harder to verify (no separable transcript to
   run `quote_in_transcript` against). **Rule:** if Gemini ever feeds extraction, the reviewer
   must remain a *different* family — never let one model both produce and judge the same quote.

   (Transcription-only does *not* dodge the non-determinism problem in §Trust — a re-transcription
   still may not be verbatim-identical — but it contains the blast radius to one well-understood
   step and leaves the two-model verification untouched.)

---

## C. Cost shift (back-of-envelope)

**Rate inputs — confirmed against Google's [pricing page](https://ai.google.dev/gemini-api/docs/pricing)
directly (Standard/paid tier; read 2026-07-10):**

| Model | Input — text/image/**video** | Input — **audio** | Output |
|---|---|---|---|
| **Gemini 2.5 Flash** | **$0.30 / 1M** | **$1.00 / 1M** | $2.50 / 1M |
| **Gemini 2.5 Flash-Lite** | **$0.10 / 1M** | **$0.30 / 1M** | $0.40 / 1M |
| Gemini 2.5 Pro | $1.25 / 1M (≤200k), $2.50 (>200k) | — | $10 / 1M (≤200k), $15 (>200k) |

- **Key confirmation:** a YouTube URL is **video** input, billed at the **$0.30/1M (Flash) /
  $0.10/1M (Flash-Lite)** rate — *not* the higher audio rate. The **$1.00/1M audio** rate only
  applies to audio-*only* inputs (e.g. an mp3 passed as `audio`), which matters for the
  full-unification option below.
- Gemini **video tokenization**: **~300 tokens/sec** at default media resolution, **~100
  tokens/sec** at low resolution (`media_resolution: low`); the audio track is ~32 tok/sec of
  that. Source:
  [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/video-understanding).
- Groq baseline: **whisper-large-v3-turbo = $0.04 / hr** audio; yt-dlp effectively free. Source:
  [Groq — Whisper Large v3 Turbo](https://groq.com/blog/whisper-large-v3-turbo-now-available-on-groq-combining-speed-quality-for-speech-recognition).

**Per hour of video (transcript output ≈ 12k tok/hr; video billed at the $0.30/$0.10 rate):**

| | input tok/hr | input $ | + output $ | **≈ $/hr** |
|---|---|---|---|---|
| **Flash-Lite, low** media-res (100 tok/s) | 360k | $0.036 | $0.005 | **~$0.04** |
| Flash-Lite, default (300 tok/s) | 1.08M | $0.108 | $0.005 | ~$0.11 |
| **Flash, low** media-res (100 tok/s) | 360k | $0.108 | $0.030 | **~$0.14** |
| Flash, default (300 tok/s) | 1.08M | $0.324 | $0.030 | ~$0.35 |
| **Groq turbo (baseline)** | — | — | — | **$0.04** |

So **Flash-Lite at low-res ≈ Groq ($0.04/hr)** — the cost objection essentially disappears if its
transcription quality is adequate (unknown — a smaller model may transcribe worse; the spike in §D
should compare quality, not just cost). **Flash** low-res is ~3.5× Groq; default-res ~9×. All are
cents/hour. For transcription we don't need visual detail → **use `media_resolution: low`.**
*(Footnote: the ~$0.30/$0.10 rate treats the whole video — including its audio track — as "video."
If Google instead meters the in-video audio at the $1.00 audio rate, add ~$0.08/hr to the Flash
rows; still cents.)*

**Monthly estimate.** Assumptions (rescale as needed): daily cron; discovery caps
`days_lookback: 7`, `max_items_per_run: 25` (`config.json`); **~3 YouTube videos/day** pass triage
to transcription, avg **45 min** (0.75 h). **The reviewer re-ingests, so each video transcribes
twice** (×2) unless cached.

- Volume: 3 × 30 = 90 videos/mo × 0.75 h = **67.5 h/mo**, ×2 re-ingest = **135 h/mo**.
- **Groq (if YouTube worked): 135 × $0.04 ≈ $5.4/mo** — but today $0 realized (blocked by #32).
- **Gemini Flash-Lite low-res: 135 × $0.04 ≈ $5/mo** (≈ Groq). **Flash low-res: 135 × $0.14 ≈
  $19/mo**; Flash default-res ≈ **$47/mo**.
- **With transcript caching (no re-transcribe on review): halve** → Flash ≈ $9/mo, Flash-Lite ≈ $3/mo.

**Δ = making the YouTube path actually work costs ≈ $0–5/mo on Flash-Lite (≈ Groq-parity) or
≈ +$5–20/mo on Flash** (vs. the theoretical-but-unrealizable Groq cost). Extraction/triage/reviewer
LLM spend is unchanged. **With the real numbers in hand, cost is *not* the blocker — the
non-determinism and recent-video reliability are.** (Show-your-work so the maintainer can re-run
with real volume: `$/mo ≈ videos_per_day × 30 × avg_hours × re_ingest_factor × $/hr`.)

### Consolidated options matrix (all on the same volume: 67.5 YouTube-h/mo base)

$/hr = input video-tokens + ~$0.03/hr transcript output. Monthly = `$/hr × 67.5 × re-ingest`
(×2 if the reviewer re-transcribes, ×1 if the transcript is cached). **Rank by dollars only —
the trust column is what actually decides.**

| # | Option | model / res | $/hr | ×2 (re-ingest) | ×1 (cached) | Trust / notes |
|---|---|---|---|---|---|---|
| 0 | **#32 fix: yt-dlp cookies/proxy + keep Groq** | Whisper turbo | $0.04 | **~$5/mo** | n/a | ✅ deterministic, verbatim, independent re-ingest all intact. Lowest risk. (+ maybe $0–10/mo if a residential proxy is used.) |
| 1 | **Gemini YouTube, Flash-Lite, low-res** *(cheapest Gemini)* | Flash-Lite / low | ~$0.04 | **~$5/mo** | ~$3/mo | ⚠️ ≈ Groq cost, but a smaller model — **transcription quality unverified**; test in §D before trusting it. |
| 2 | **Gemini YouTube, Flash, low-res** *(recommended if Gemini)* | Flash / low | ~$0.14 | ~$19/mo | **~$9/mo** | ⚠️ non-deterministic transcript → needs fuzzy-match or cache; recent-video flakiness. |
| 3 | Gemini YouTube, Flash, default-res | Flash / default | ~$0.35 | ~$47/mo | ~$24/mo | Same risks; no quality reason to pay this for transcription. |
| 4 | Gemini YouTube, **Pro**, low-res | Pro / low | ~$0.57 | ~$77/mo | ~$38/mo | ❌ overkill for ASR; Pro buys reasoning we don't use here. |
| 5 | **Full unification** (podcasts+direct audio → Gemini too) | Flash / audio 32 tok/s @ $1.00 | ~$0.15* | ~$20/mo* | ~$10/mo* | ❌ audio-only is billed at the **$1.00/1M audio rate** → ~4× Groq, *and* trades today's **working, deterministic** Groq path for a non-deterministic one for **no benefit**. Not recommended. |

\* Audio-*only* (a podcast mp3 passed as `audio`, not a YouTube URL) tokenizes at ~32 tok/s but
bills at the **$1.00/1M audio** rate → ~32k×3600÷1e6×$1.00 ≈ **$0.12/hr** + output ≈ $0.15/hr,
i.e. ~4× Groq — the opposite of cost-neutral, and the wrong trade regardless (see #5).

**Two things the matrix makes clear:**
- **The pipeline-depth axis (transcription-only vs transcription+analysis) is ~cost-neutral.**
  If Gemini also extracted, you'd save only the DeepSeek extractor call — negligible *text*
  tokens (a transcript in, small JSON out, ≈ $0.001/episode). So that axis is a **trust decision,
  not a cost one** (§B axis 2). Don't pick transcription+analysis to "save money" — there's none.
- **Native vs OpenRouter:** OpenRouter bills Gemini at ~Google list price (its margin is a small
  credit-purchase fee, not a per-token markup), so options 1–4 are ≈ the same either way — the
  OpenRouter route's advantage is operational (**no new secret**), not cost. Confirm on the
  [OpenRouter Gemini model page](https://openrouter.ai/google/gemini-2.5-flash) before relying on
  exact parity.

---

## Trust-model handling (the hard part)

**Both Groq-Whisper and Gemini are generative transcription models** — the difference isn't
"LLM vs not." It's that **Groq-Whisper is reproducible run-to-run and verbatim-by-design** (a
dedicated ASR at temp-0, whose objective is to reproduce the spoken words), whereas **Gemini's
transcript varies across calls** (it samples output and re-fetches/re-samples the video each time)
**and may not be strictly verbatim** (a general model may normalize disfluencies or lightly
paraphrase). So a re-transcription in `review.yml` may not reproduce the extractor's exact wording,
and `quote_in_transcript`'s exact-substring check **would start false-flagging real quotes**.
Today's audio path already carries a *small* version of this drift; Gemini **enlarges** it (and
weakens the verbatim guarantee) rather than introducing a brand-new failure mode. Options,
least-to-most trust-preserving:

1. **Strict fuzzy matcher (recommended default).** Replace exact substring with a *tight*
   normalized match (e.g. require the quote to appear as a near-contiguous span with ≥~95%
   token-sequence overlap). **Preserves independent re-transcription** (reviewer still re-fetches
   via Gemini, different pass). Risk: loosens the verbatim guarantee; must stay strict enough that
   a fabricated/mis-attributed quote still fails. Add tests with adversarial near-misses.
2. **Cache the extractor's transcript for the reviewer.** The reviewer verifies against the *same*
   transcript the extractor used (passed as a PR/CI artifact — **not committed**, per the
   copyright rule that keeps transcripts out of the repo). Keeps exact-substring + halves cost,
   **but weakens "independent re-ingest"** — the reviewer no longer re-fetches the source. The
   different-family reviewer LLM still independently judges faithfulness/attribution, so it's a
   *reasonable* trade, not a free one. Document the downgrade explicitly.
3. **Timestamped/grounded transcript** — request timestamps and store the cited span; still
   non-deterministic text, so pair with (1) or (2).

**Do not** simply trust the model's word (drop the deterministic check) — that removes the guard
that also blocks path-injection via extractor output (see the Security note in `CLAUDE.md`).
Keep #2 (different-family reviewer) and #3 (human review) untouched. Also weigh that Gemini
transcripts **may not be truly verbatim** — if it cleans up disfluencies, a quote the extractor
pulls may never have been said word-for-word. That is a first-order threat to the project's
premise and is the main reason this is a *conditional* go.

---

## D. Proposed CI de-risking spike (for a future session — NOT run here)

The only way to truly answer "works in CI/CD" and "handles fresh videos" is one real run from a
GitHub-Actions runner. Keep it **throwaway (scratch branch / `workflow_dispatch`), not merged.**

1. **Prefer the OpenRouter route first** (reuses `OPENROUTER_API_KEY`, no new secret). Add a
   scratch `workflow_dispatch` job that POSTs to OpenRouter `/chat/completions` with a
   `video_url` part = a **public YouTube URL**, `model: google/gemini-2.5-flash`,
   `provider: {order: ["google-ai-studio"]}`, prompt "Transcribe verbatim." (Native fallback:
   a `GEMINI_API_KEY` secret the maintainer adds + `fileData`/`file_uri`.)
2. **Test the real risk, not a soft one:** use a **video < 1 week old** (a fresh candidate clip),
   because recent-video fetch is the documented failure mode. Also test a 60–90 min forum.
3. **Assert:** non-empty transcript **and** a known verbatim phrase from the video appears
   (normalized). Log token usage → real $/episode. Record latency and any `400 INVALID_ARGUMENT`
   retries.
4. **Pass/fail:** GO only if fresh videos transcribe reliably across a few tries **and** a strict
   matcher (§Trust option 1) still matches across two independent transcriptions of the same
   video. Otherwise NO-GO — fall back to the #32 cookies/proxy fix.

Command shape (once the scratch workflow exists):
`gh workflow run gemini-spike.yml --ref <scratch-branch> -f url=<fresh public YouTube URL>` → read
the job log for the transcript + token/cost lines.

---

## E. Recommended evals before committing (price × accuracy × complexity)

§D proves *"does it fetch in CI."* This §E is the complementary question: *"is the transcript good
and reproducible enough, and is it worth it?"* The go/no-go above rests on *documented* risks; a
small eval turns them into measured numbers. This is **what to compare and why — not how to build
it.**

**Configs to put side by side** (same handful of real Chicago-mayoral sources — a few YouTube
clips + a couple podcasts):
- **Groq Whisper turbo** — control/baseline (what we ship today).
- **Gemini 2.5 Flash-Lite** — cheapest (~Groq parity on cost).
- **Gemini 2.5 Flash** — mid-tier.
- *(Optional)* the **#32 cookies/proxy path** — accuracy identical to Groq (it *is* Groq); differs
  only on reliability + complexity, so it's a useful "no-accuracy-risk" reference point.

**Axes to measure:**
1. **Accuracy / verbatim fidelity** — on clips with a trusted reference transcript. Weight a
   project-specific metric over raw WER: **quote-recall** — do the specific *housing* quotes
   survive verbatim, i.e. would `quote_in_transcript` still match? A model can have great overall
   WER and still paraphrase the one sentence we cite.
2. **Reproducibility** — transcribe each source **twice**; measure how often the same quote fails
   to re-match. This directly sizes the trust-model risk (the reviewer re-ingests → drift = false
   flags on real quotes).
3. **Recent-video reliability** — fetch-success rate on **fresh** (<1 wk / <1 mo) public videos
   vs. older ones (overlaps §D; the single biggest CI unknown).
4. **Measured cost/episode** — log real token usage to replace the back-of-envelope with actual $.

**Complexity** isn't an eval — it's a judgment (new seams, lines changed, whether the offline
suite stays fixture-only). Score it qualitatively alongside the three measured axes.

**Scope:** the **lightweight version** (axes 1–3 on ~5–10 clips across the three transcriber
configs) is cheap and targets the two decision-blockers → do it **first**. A **full end-to-end
eval** (same sources through the whole extract→review pipeline, comparing final proposed stances +
reviewer verdicts per transcriber) is higher effort and can wait until the lightweight pass clears.
Output: a small **price × accuracy × complexity** table to decide on.

---

## Recommendation

- **Conditional GO to de-risk; NO-GO on committing until the §D spike + §E evals clear the
  recent-video and verbatim-reproducibility risks.** The mechanism is real and cheap, and
  OpenRouter passthrough means no new secret — but the two project-specific risks are exactly the
  ones that would silently degrade trust or re-break YouTube through a different door. Decide with
  the §E price × accuracy × complexity table in hand, not on the documented risks alone.
- **If adopted:** hybrid scope on **both** axes — Gemini for **YouTube only** (Groq stays for
  podcasts/direct audio) and **transcription only** (DeepSeek extractor + different-family Kimi
  reviewer unchanged; Gemini never selects or judges quotes). Plus `media_resolution: low`,
  retries on transient 400s, and a **strict fuzzy matcher** (or cached transcript) so
  `quote_in_transcript` survives non-determinism — with tests.
- **Lower-risk alternative to weigh first:** the **#32 cookies/proxy fix for yt-dlp** keeps the
  entire deterministic, verbatim, independent-re-ingest trust model intact and is a smaller
  change. Gemini is the bigger bet; pick it only if the spike shows it's clearly more reliable
  for *fresh* YouTube than cookies/proxy.

## Sources
- [Video understanding | Gemini API](https://ai.google.dev/gemini-api/docs/video-understanding) ·
  [File input methods](https://ai.google.dev/gemini-api/docs/file-input-methods) ·
  [Pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [OpenRouter — Video Inputs](https://openrouter.ai/docs/guides/overview/multimodal/videos) ·
  [OpenRouter — Multimodal overview](https://openrouter.ai/docs/guides/overview/multimodal/overview)
- [Groq — Whisper Large v3 Turbo](https://groq.com/blog/whisper-large-v3-turbo-now-available-on-groq-combining-speed-quality-for-speech-recognition)
- [artificialanalysis — Gemini 2.5 Flash](https://artificialanalysis.ai/models/gemini-2-5-flash) ·
  [2.5 Pro](https://artificialanalysis.ai/models/gemini-2-5-pro)
- Reliability: [Recent videos inaccessible](https://discuss.ai.google.dev/t/recent-youtube-videos-inaccessible-via-gemini-3-flash-preview-api/114076) ·
  [Certain links don't work](https://discuss.ai.google.dev/t/certain-youtube-links-dont-work/88350) ·
  [Intermittent 400 on YouTube URL](https://discuss.ai.google.dev/t/youtube-url-video-input-returns-intermittent-400-invalid-argument-should-be-503-or-retryable-error-code/125883)

*Capability/pricing claims verified against sources fetched 2026-07-10; model APIs drift — re-check
before implementing.*
