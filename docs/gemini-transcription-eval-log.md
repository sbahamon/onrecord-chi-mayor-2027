# Gemini YouTube transcription — live eval + decision log

**Status: NO-GO on adopting Gemini as the general YouTube transcription path (verdict airtight
after a native-SDK low-res retest). Recommend #32 (yt-dlp cookies/proxy so the existing Groq path
works on YouTube). BUT: Gemini is a viable *short-clip* fallback if #32 fails — see
[§ Fallback](#fallback-if-32-fails-gemini-for-short-clips-only).** Local eval 2026-07-10.

This log records the trial method + full measured results so the call is reproducible and can (a)
seed the site methodology page and (b) let a future session resurrect the short-clip Gemini path
without re-deriving anything. It stores **only** method + short quote snippets + metrics — never
full transcripts (copyright; golden rule #4). The throwaway eval harness is preserved at
[`evals/gemini_transcription/`](../evals/gemini_transcription/) so any of this is re-runnable.

Follows the feasibility research in
[`gemini-youtube-transcription-research.md`](./gemini-youtube-transcription-research.md).
Issues: #36 (this idea), #32 (YouTube bot-gate — the fallback we now recommend).

---

## TL;DR

Passing a YouTube URL to Gemini **works and cleanly fetches fresh videos** (the documented
recent-video risk did **not** bite on <2-day clips), and Gemini transcribes **short** clips well
and cheaply. But it **fails on long-form content — the housing forums/interviews that carry the
substance** — and a native-SDK retest proved that failure is **not** just a cheap-config artifact:

- **Phase 1 (OpenRouter):** 49-min interview → runaway output; 88-min debate → hard fail
  (exceeded the 1,048,576-token context because OpenRouter **silently ignored `media_resolution:
  low`**, so video tokenized at ~295 tok/s).
- **Phase 2 (native google-genai SDK, low-res forced):** low-res **did** clear the 88-min ingest
  ceiling (547k tokens < 1M; ~103 tok/s) — so *that* failure was an OpenRouter artifact. **But the
  long transcripts were non-reproducible and unstable anyway**: the 88-min clip gave 54k chars one
  run and 290k (3.2× runaway) the next — **0% reproducibility**. Flash-Lite couldn't complete the
  long clips (timeouts / 400s), and `gemini-2.5-flash-lite` is now **sunset for new API keys**.

Groq, with its existing chunking, transcribed **every** length reliably and deterministically.
The runaway/non-reproducibility is an **output-side** failure resolution can't fix, and it breaks
the exact-substring `quote_in_transcript` trust check on exactly the content we most need.
**Leans decisively Groq-heavy → do #32.**

---

## Why this was safe to test locally (no #32 needed)

The #32 bot-gate is **datacenter-IP-only** — yt-dlp gets "Sign in to confirm you're not a bot"
from GitHub-Actions runner IPs, but works fine from a laptop. So the **Groq baseline ran locally**
on the same YouTube URLs (yt-dlp → ffmpeg downsample → Whisper, the shipping path unchanged), and
Gemini ran via API. No new secret was needed for Phase 1 (reused `OPENROUTER_API_KEY`); Phase 2
needed a `GEMINI_API_KEY`.

## Clips (all verified live via yt-dlp before use — the curating subagent is untrusted)

| id | candidate | age (of 2026-07-10) | length | outlet | url |
|---|---|---|---|---|---|
| quigley-fresh | Mike Quigley | **1 day** | 4:36 (276s) | CBS | youtube.com/watch?v=LJO16xf9yG8 |
| nee-fresh | Lisa Nee | **2 days** | 4:30 (270s) | CBS | youtube.com/watch?v=1HRzF5DG1FQ |
| brewer-recent | Matt Brewer | 8 days | 10:39 (639s) | FOX32 | youtube.com/watch?v=fNfbfNS8eFI |
| johnson-older | Brandon Johnson | ~12 mo | 49:26 (2966s) | WBEZ | youtube.com/watch?v=KQ61-Mpo2wA |
| abc7-debate-long | Johnson v Vallas | ~3 yr | 88:27 (5307s) | ABC7 | youtube.com/watch?v=ck8GPV5Rtpo |

---

## Method

- **Transcribers:** Groq Whisper `whisper-large-v3-turbo` (trusted reference = what ships today)
  vs Gemini **2.5 Flash** and **Flash-Lite**, `temperature: 0`, each run **twice** per clip.
- **Phase 1 route:** OpenRouter `/chat/completions`, `video_url` content part,
  `provider:{order:["google-ai-studio"]}`, `media_resolution: low` *requested*.
- **Phase 2 route:** native **google-genai** SDK — `file_data`/`file_uri` = the YouTube URL,
  `GenerateContentConfig(media_resolution=MEDIA_RESOLUTION_LOW, max_output_tokens=65535)` — to
  **force** low-res (OpenRouter dropped it). Long clips only.
- **Axes:** fresh-video fetch; length reliability; run-to-run reproducibility; quote-recall vs
  Groq; real cost from reported token usage.

**Metric caveat (read before trusting the recall column):** quote-recall sampled whole sentences
(≥8 words) at ≥95% token-identity. Two independent ASR systems differ by a few
words/contractions per sentence, so this bar reads ~0% *even when content is faithful* (measured
cross-Groq fuzzy ratios were **0.80–0.94**, not near-zero). So recall-vs-Groq mainly demonstrates
that **exact-substring matching does not survive a change of transcription model** — expected, and
the reason a fuzzy matcher would be mandatory under any transcriber swap. It is **not** evidence
Gemini transcribes short clips badly (it doesn't — see Quality). The deciding axes (fetch, length,
reproducibility, cost) don't depend on it.

---

## Results by axis

### 1. Recent-video fetch reliability — **PASS**
Both **<2-day-old** clips transcribed on every Gemini call (4/4 each). The documented "recent
videos fail via the API" problem did not appear. Caveat: n=2 — encouraging, not proven at scale.

### 2. Length reliability — **FAIL for long-form (the decider)**
| length | Phase 1 (OpenRouter, default-res ~295 tok/s) | Phase 2 (native SDK, low-res ~103 tok/s) |
|---|---|---|
| ≤5 min | clean, correct length, both models | (not re-run; already good) |
| ~11 min | Flash: 1 `524` gateway timeout (retryable), else fine; Flash-Lite fine | — |
| **49 min** | runaway: Flash ~2×, Flash-Lite ~**6×** real length | Flash: run1 47,116c ✅ / run2 32,714c (truncated) → **repro 50%**; Flash-Lite: connection-reset + read-timeout |
| **88 min** | **all fail** — "input token count exceeds 1,048,576" | **ingests now** (prompt 546,729 tok) but Flash run1 54,290c (dropped ~40%) / run2 **290,277c (3.2× runaway)** → **repro 0%**; Flash-Lite: `400 INVALID_ARGUMENT` |

**Phase 2 is the crux:** forcing low-res *raised the ingest ceiling* (default-res ~295 tok/s →
~59-min ceiling; low-res ~103 tok/s → ~175-min ceiling), **but long-form transcription is
non-reproducible and unstable regardless** — truncation on one run, 3.2× runaway on the next.
That's an **output-side** failure resolution cannot fix. Groq transcribed all five lengths (incl.
the 88-min, chunked) correctly and deterministically.

### 3. Reproducibility — Flash-Lite good on short; everything unstable on long
Phase 1, temp 0: **Flash-Lite byte-identical run-to-run on short/mid clips** (reproducible);
**Flash was not** (Quigley Flash run1 4,664c ≠ run2 4,574c — nondeterministic even at temp 0; not
caching, since other Flash pairs *were* identical). On long content both models are unreliable
(0%/50% above). A transcriber that re-transcribes differently on the reviewer's re-ingest would
false-flag real quotes.

### 4. Quality — good on short clips that complete
Spot-read vs Groq: faithful, readable; differences cosmetic ("5th"↔"Fifth", casing, punctuation)
plus minor ASR slips — **Flash-Lite slightly worse** (rendered "Rahm Emanuel" as "Rom Emanuel").
Quality is **not** the blocker; long-form length-reliability + non-reproducibility are.

### 5. Cost — measured both routes
Audio bills **separately at the $1.00/1M audio rate** (video_tokens@$0.30 + audio_tokens@$1.00 =
the reported prompt cost, confirmed exactly). `media_resolution: low` is **honored natively**
(~71 video tok/s) but **ignored via OpenRouter** (~295 tok/s). Google list $/1M used: Flash
video/audio/out = 0.30 / 1.00 / 2.50; Flash-Lite = 0.10 / 0.30 / 0.40.

| transcriber | route / res | measured $/hr | ~$/mo (135 h, 2× re-ingest) |
|---|---|---|---|
| **Groq Whisper** (today) | — | **$0.04** | ~$5 (once #32 unblocks fetch) |
| Gemini Flash | OpenRouter, default-res | ~$0.35 | ~$47 |
| Gemini Flash | **native, low-res** | **~$0.22** | ~$30 |
| Gemini Flash-Lite | OpenRouter, default-res | ~$0.11 | ~$15 |

Native low-res roughly halves Flash's cost (audio is the irreducible floor — you can't drop it
without downloading the audio yourself, which re-hits #32). Cost is moot here since nothing
cleared the reliability bar. **Model-availability gotcha:** `gemini-2.5-flash-lite` (the cheap
tier the research doc costed) is **sunset for new API keys** ("no longer available to new users") —
a new project must use `gemini-flash-lite-latest` / a newer gen, which failed the long clips here.

---

## Decision (two-gate rule: trust first, then price)

**Gate 1 (trust):** fresh fetch ✅, short-clip quality ✅, Flash-Lite short-clip reproducibility ✅
— **but long-form reliability ❌ for both models on both routes**, and low-res (the last plausible
fix) cleared only the *ingest ceiling*, not the runaway/non-reproducibility. The tracker's
highest-value housing evidence is long forums/interviews — exactly where Gemini breaks. **Gate 1
fails for a general path.**

**Gate 2 (price):** moot — nothing cleared Gate 1.

**Verdict: NO-GO on Gemini as the *general* YouTube transcription path.** → **Pursue #32 (yt-dlp
cookies/proxy)** so the deterministic Groq path (all lengths, verbatim, reproducible) works on
YouTube in CI. Keep Groq for podcasts/direct audio unchanged.

---

## Fallback: if #32 fails, Gemini for SHORT clips only

If the #32 cookies/proxy approach proves unworkable (e.g. proxies get bot-gated too, or cookies
rot), Gemini is a **usable fallback for short YouTube clips** — the news hits and short segments,
*not* forums. What the eval established for that path:

- **Works well ≤~11 min:** every clip ≤10:39 transcribed cleanly, correct length. Flash-Lite was
  byte-reproducible; quality good. Fresh videos fetched fine.
- **Untested gap 11–49 min:** the first failure we observed was at 49 min. A conservative gate
  would cap at **~15 min** and treat 15–49 min as unknown (test before trusting).
- **Do NOT use ≥~49 min:** runaway/truncation/non-reproducibility (documented above).

**To build it (all deferred until actually needed):**
1. **Route by length + type:** in the discovery/ingest path, send only `youtube` items under the
   duration cap to Gemini; everything else stays on Groq. (Groq already handles podcasts/direct
   audio; keep it.)
2. **Use the native SDK path** (low-res honored, cheaper, higher ceiling) — the working call is in
   `evals/gemini_transcription/run_eval.py::gemini_transcribe_native`. Pin to an available model
   (`gemini-2.5-flash`; **not** `gemini-2.5-flash-lite` — sunset for new keys). Add a retry for the
   intermittent `524`/`400`/read-timeout.
3. **Add a strict fuzzy matcher** to `quote_in_transcript` — mandatory, because a Gemini transcript
   is not byte-identical to Groq, and even Gemini-vs-Gemini drifts. Keep it strict enough that a
   fabricated/mis-attributed quote still fails; keep the deterministic check as the path-injection
   guard (see CLAUDE.md security note). Add adversarial near-miss tests.
4. **Keep the two-model design:** Gemini transcribes only; DeepSeek extracts; Kimi reviews. Never
   let Gemini both produce and judge a quote.
5. **Needs a `GEMINI_API_KEY` secret** in `cron`/`review`/`intake` workflows.

Cost for the short-clip fallback is trivial (short clips are a few cents each even at Flash rates).

---

## How to reproduce

Harness: [`evals/gemini_transcription/run_eval.py`](../evals/gemini_transcription/) (+ `clips.json`).
Needs `.venv` with `requests`, `yt-dlp`, ffmpeg, and — for the native path —
`pip install google-genai`. Load keys: `set -a && . ./.env && set +a`.

```bash
# Phase 1 (OpenRouter, all 5 clips):   OPENROUTER_API_KEY
python evals/gemini_transcription/run_eval.py --smoke        # validate video_url schema
python evals/gemini_transcription/run_eval.py                # full matrix

# Phase 2 (native SDK, low-res, long clips):   GEMINI_API_KEY
python evals/gemini_transcription/run_eval.py --native-smoke # confirm low-res (~100 tok/s)
python evals/gemini_transcription/run_eval.py --native-long  # 2 long clips, flash + flash-lite
```
Raw transcripts + `summary.json` go to `$EVAL_RESULTS_DIR` (default: system temp) — **never** the
repo. The script mirrors `pipeline/extract.py::_normalize` for the exact-substring metric.

## Appendix — full per-run data
Groq reference char counts: quigley 4,582 · nee 5,033 · brewer 9,831 · johnson 46,977 · abc7 90,903.

**Phase 1 (OpenRouter, default-res ~295 tok/s), chars run1/run2, $/hr:**
- quigley (276s): Flash 4,664/4,574 ($0.35); Flash-Lite 4,535/4,535 byte-identical ($0.11)
- nee (270s): Flash 5,080/5,080 ($0.36); Flash-Lite 5,136/5,136 ($0.11)
- brewer (639s): Flash **524 timeout**/10,335 ($0.35); Flash-Lite 10,046/10,046 ($0.11)
- johnson (2966s): Flash 94,240/46,950 (repro 16.7%); Flash-Lite 291,585/**premature-end fail**
- abc7 (5307s): **all 4 fail — token limit 1,048,576**

**Phase 2 (native SDK, low-res ~71 video tok/s):**
- smoke nee (270s) Flash: 5,106c, video 19,241 tok (71.3/s), audio 8,655, prompt 27,929 → low-res confirmed
- johnson (2966s): Flash 47,116/32,714 (prompt 305,531; $0.22/hr; repro 50% exact / 66.7% fuzzy);
  Flash-Lite `gemini-flash-lite-latest` → connection-reset / read-timeout
- abc7 (5307s): Flash 54,290/290,277 (prompt 546,729; $0.21–0.30/hr; **repro 0%**);
  Flash-Lite → `400 INVALID_ARGUMENT` ×2
- `gemini-2.5-flash-lite` native → `404` sunset

Total Gemini spend across both phases ≈ **$2.5**.
