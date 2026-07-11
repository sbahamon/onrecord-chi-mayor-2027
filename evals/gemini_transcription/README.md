# Gemini YouTube transcription eval (throwaway spike, preserved)

One-off harness that measured whether YouTube transcription could route through **Gemini** (URL →
Google fetches it, bypassing the #32 yt-dlp bot-gate) instead of Groq. **Not production code** —
kept for reproducibility and in case the short-clip fallback is ever needed.

**Verdict + full results:** [`../../docs/gemini-transcription-eval-log.md`](../../docs/gemini-transcription-eval-log.md)
(NO-GO as a general path — long-form transcription is non-reproducible/runaway; Gemini is a viable
*short-clip* fallback only if #32 fails). Feasibility background:
[`../../docs/gemini-youtube-transcription-research.md`](../../docs/gemini-youtube-transcription-research.md).

## Run
```bash
set -a && . ../../.env && set +a          # OPENROUTER_API_KEY, GROQ_API_KEY, (GEMINI_API_KEY for --native-*)
python run_eval.py --smoke                # validate OpenRouter video_url schema (1 short clip)
python run_eval.py                        # full matrix: 5 clips × {Groq, Flash, Flash-Lite} ×2 runs
python run_eval.py --native-smoke         # confirm native SDK low-res is honored (~100 tok/s)
python run_eval.py --native-long          # native low-res retest, 2 long clips  (needs: pip install google-genai)
```
Raw transcripts → `$EVAL_RESULTS_DIR` (default: system temp). **Never committed** (copyright —
only extracted quotes + metrics live in the decision log). `clips.json` holds the 5 verified URLs.
