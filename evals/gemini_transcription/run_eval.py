#!/usr/bin/env python3
"""THROWAWAY eval — Gemini YouTube transcription vs Groq baseline.

NOT production code. Lives in a scratch worktree; never merged. Measures the two
risks the research doc (docs/gemini-youtube-transcription-research.md) couldn't
settle from docs alone:

  1. recent-video fetch reliability (does Gemini fetch fresh videos at all)
  2. verbatim reproducibility + quote-recall vs the trusted Groq baseline
     (would switching break the exact-substring `quote_in_transcript` guard)

plus real per-episode cost. Groq is the reference standard (what ships today).
Raw transcripts are written to a scratch dir OUTSIDE the repo — never committed
(copyright: golden rule #4). The decision log records only short quote snippets.

Run:
  set -a && . ./.env && set +a
  python evals/gemini_transcription/run_eval.py --smoke   # 1 clip, 1 model, sanity-check schema
  python evals/gemini_transcription/run_eval.py           # full matrix
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# Reuse the REAL Groq path (yt-dlp + ffmpeg downsample -> Whisper) unchanged.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from pipeline.transcribe import download_media, transcribe_audio  # noqa: E402

import requests  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

GEMINI_MODELS = {
    "flash": "google/gemini-2.5-flash",
    "flash-lite": "google/gemini-2.5-flash-lite",
}
# Google list prices, $/1M tokens (video input rate + output rate), per the
# research doc's pricing table (verify against the live pricing page before trusting).
PRICES = {
    "flash": {"in": 0.30, "out": 2.50},
    "flash-lite": {"in": 0.10, "out": 0.40},
}
GROQ_PER_HOUR = 0.04  # whisper-large-v3-turbo baseline

# Native google-genai SDK path (Phase 2): lets us FORCE media_resolution=LOW,
# which OpenRouter silently ignored. Model ids differ from the OpenRouter slugs.
NATIVE_MODELS = {
    "flash": "gemini-2.5-flash",
    # NOTE: gemini-2.5-flash-lite is sunset ("no longer available to new users") on a
    # fresh API key — the exact model the research doc costed can't be used by new projects.
    # gemini-flash-lite-latest is the available flash-lite (may resolve to a newer gen).
    "flash-lite": "gemini-flash-lite-latest",
}
# Google list prices, $/1M. Video frames + the audio track bill at DIFFERENT rates
# (confirmed in Phase 1: audio meters at the higher audio rate), so cost is computed
# from the per-modality token breakdown, not a single input rate.
NATIVE_RATES = {
    "flash": {"video": 0.30, "audio": 1.00, "out": 2.50},
    "flash-lite": {"video": 0.10, "audio": 0.30, "out": 0.40},
}

TRANSCRIBE_PROMPT = (
    "Transcribe the spoken words in this video verbatim, word for word. "
    "Output ONLY the transcript text — no timestamps, no speaker labels, no "
    "commentary, no summary."
)

HOUSING_RE = re.compile(
    r"\b(housing|rent|renter|afford|zoning|zone[ds]?|develop|property tax|"
    r"homeowner|tenant|evict|homeless|construct|build|landlord|mortgage|"
    r"gentrif|density|unit)\b",
    re.I,
)


# ---- matching (mirror pipeline/extract.py exactly) -------------------------
def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def exact_in(quote: str, transcript: str) -> bool:
    """The production check: whitespace-collapsed, casefolded substring."""
    return normalize(quote) in normalize(transcript)


def fuzzy_best_ratio(quote: str, transcript: str) -> float:
    """Best token-sequence overlap of `quote` against any equal-length window."""
    q = normalize(quote).split()
    t = normalize(transcript).split()
    if not q:
        return 0.0
    n = len(q)
    if len(t) < n:
        return difflib.SequenceMatcher(None, q, t).ratio()
    best = 0.0
    for i in range(len(t) - n + 1):
        r = difflib.SequenceMatcher(None, q, t[i:i + n]).ratio()
        if r > best:
            best = r
            if best >= 0.999:
                break
    return best


FUZZY_THRESHOLD = 0.95


# ---- quote sampling from the Groq reference transcript ---------------------
def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    return [p.strip() for p in parts if p.strip()]


def sample_quotes(reference: str, *, want: int = 6) -> list[str]:
    """Pull housing-relevant sentences (>=8 words) from the reference transcript.

    Falls back to the longest distinctive sentences if too few housing hits, so
    quote-recall is always measurable even on a clip light on housing talk.
    """
    sents = split_sentences(reference)
    housing = [s for s in sents if len(s.split()) >= 8 and HOUSING_RE.search(s)]
    seen, picked = set(), []
    for s in housing:
        key = normalize(s)
        if key not in seen:
            seen.add(key)
            picked.append(s)
        if len(picked) >= want:
            break
    if len(picked) < 3:  # supplement with long generic sentences
        for s in sorted(sents, key=lambda x: -len(x.split())):
            key = normalize(s)
            if key not in seen and len(s.split()) >= 10:
                seen.add(key)
                picked.append(s)
            if len(picked) >= want:
                break
    return picked


def recall(quotes: list[str], transcript: str) -> dict:
    if not quotes:
        return {"n": 0, "exact": 0, "fuzzy": 0, "exact_pct": None, "fuzzy_pct": None,
                "per_quote": []}
    per = []
    ex = fz = 0
    for q in quotes:
        e = exact_in(q, transcript)
        r = fuzzy_best_ratio(q, transcript)
        f = r >= FUZZY_THRESHOLD
        ex += e
        fz += f
        per.append({"quote": q[:120], "exact": e, "fuzzy_ratio": round(r, 3),
                    "fuzzy_pass": f})
    n = len(quotes)
    return {"n": n, "exact": ex, "fuzzy": fz,
            "exact_pct": round(100 * ex / n, 1), "fuzzy_pct": round(100 * fz / n, 1),
            "per_quote": per}


# ---- transcribers ----------------------------------------------------------
def groq_transcribe(url: str) -> dict:
    t0 = time.time()
    path = download_media(url)
    try:
        text = transcribe_audio(path)
    finally:
        pass  # leave downsampled file in the temp dir; runner/OS reclaims it
    return {"ok": bool(text), "text": text, "latency": round(time.time() - t0, 1)}


def gemini_transcribe(url: str, model_key: str, api_key: str) -> dict:
    model_id = GEMINI_MODELS[model_key]
    body = {
        "model": model_id,
        "provider": {"order": ["google-ai-studio"], "allow_fallbacks": False},
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                # media_resolution: low keeps video tokens ~100 tok/s (vs ~300);
                # if OpenRouter ignores it, the token count reveals it — we log either way.
                {"type": "video_url",
                 "video_url": {"url": url, "media_resolution": "low"}},
            ],
        }],
    }
    t0 = time.time()
    try:
        resp = requests.post(
            OPENROUTER,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "Chicago Housing Tracker - gemini eval",
            },
            json=body,
            timeout=900,
        )
    except Exception as e:
        return {"ok": False, "error": f"request exception: {e}",
                "latency": round(time.time() - t0, 1)}
    dt = round(time.time() - t0, 1)
    status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "status": status, "error": resp.text[:600], "latency": dt}
    if status != 200 or "choices" not in data:
        return {"ok": False, "status": status,
                "error": json.dumps(data)[:600], "latency": dt}
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage", {}) or {}
    return {"ok": bool(text.strip()), "status": status, "text": text,
            "usage": usage, "latency": dt}


def cost_of(model_key: str, usage: dict) -> dict:
    p = PRICES[model_key]
    pin = usage.get("prompt_tokens") or 0
    pout = usage.get("completion_tokens") or 0
    computed = pin / 1e6 * p["in"] + pout / 1e6 * p["out"]
    reported = usage.get("cost")  # OpenRouter sometimes reports actual credits
    return {"prompt_tokens": pin, "completion_tokens": pout,
            "computed_usd": round(computed, 5),
            "reported_usd": reported}


# ---- native google-genai SDK path (Phase 2, forces low-res) ----------------
def gemini_transcribe_native(url: str, model_key: str, api_key: str) -> dict:
    from google import genai
    from google.genai import types

    model_id = NATIVE_MODELS[model_key]
    # 20-min client timeout — an 88-min video is processed server-side (timeout is ms).
    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=1_200_000))
    contents = types.Content(parts=[
        types.Part(text=TRANSCRIBE_PROMPT),
        types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
    ])
    cfg = types.GenerateContentConfig(
        temperature=0,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        max_output_tokens=65535,  # don't truncate a long transcript (~17k tok for 88 min)
    )
    t0 = time.time()
    try:
        resp = client.models.generate_content(model=model_id, contents=contents, config=cfg)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "latency": round(time.time() - t0, 1)}
    dt = round(time.time() - t0, 1)
    try:
        text = resp.text or ""
    except Exception as e:
        text = ""
        # keep going — record why (e.g. blocked / no candidate)
        return {"ok": False, "error": f"no text: {e}", "latency": dt,
                "finish": str(getattr(resp, "candidates", None))[:200]}
    um = getattr(resp, "usage_metadata", None)
    video_tokens = audio_tokens = None
    details = getattr(um, "prompt_tokens_details", None) if um else None
    for d in details or []:
        mod = str(getattr(d, "modality", "")).upper()
        tc = getattr(d, "token_count", 0) or 0
        if "VIDEO" in mod:
            video_tokens = tc
        elif "AUDIO" in mod:
            audio_tokens = tc
    return {"ok": bool(text.strip()), "text": text, "latency": dt,
            "prompt_tokens": getattr(um, "prompt_token_count", None) if um else None,
            "completion_tokens": getattr(um, "candidates_token_count", None) if um else None,
            "video_tokens": video_tokens, "audio_tokens": audio_tokens}


def native_cost(model_key: str, r: dict) -> dict:
    rate = NATIVE_RATES[model_key]
    v = r.get("video_tokens") or 0
    a = r.get("audio_tokens") or 0
    o = r.get("completion_tokens") or 0
    usd = v / 1e6 * rate["video"] + a / 1e6 * rate["audio"] + o / 1e6 * rate["out"]
    return {"video_tokens": v, "audio_tokens": a, "completion_tokens": o,
            "computed_usd": round(usd, 5)}


def run_native_long(clips: list[dict], results_dir: Path, gemini_runs: int,
                    gem_key: str, models: list[str] | None = None):
    """Retest only the LONG clips at forced low-res. Reuses the Groq reference
    transcripts already captured in results_dir (from the Phase-1 run)."""
    results_dir.mkdir(parents=True, exist_ok=True)
    models = models or ["flash", "flash-lite"]
    longs = [c for c in clips if c.get("duration_sec", 0) >= 2000]
    summary = []
    for clip in longs:
        cid, url, dur_s = clip["id"], clip["url"], clip["duration_sec"]
        groq_path = results_dir / f"{cid}.groq.txt"
        groq_text = groq_path.read_text() if groq_path.exists() else ""
        quotes = sample_quotes(groq_text) if groq_text else []
        print(f"\n=== NATIVE-LOW {cid} | {dur_s}s | groq_ref={len(groq_text)}c | {url} ===",
              file=sys.stderr)
        entry = {"clip": clip, "groq_ref_chars": len(groq_text),
                 "n_quotes": len(quotes), "native": {}}
        for mk in models:
            runs = []
            for i in range(gemini_runs):
                print(f"  [{mk}] run {i+1}/{gemini_runs} ...", file=sys.stderr)
                r = gemini_transcribe_native(url, mk, gem_key)
                (results_dir / f"{cid}.{mk}.native.run{i+1}.txt").write_text(
                    r.get("text", "") if r.get("ok") else f"ERROR: {r.get('error')}")
                tps = (round(r["video_tokens"] / dur_s, 1)
                       if r.get("ok") and r.get("video_tokens") and dur_s else None)
                rec = {"ok": r["ok"], "latency": r.get("latency"), "error": r.get("error"),
                       "chars": len(r.get("text", "")),
                       "prompt_tokens": r.get("prompt_tokens"),
                       "video_tokens": r.get("video_tokens"),
                       "audio_tokens": r.get("audio_tokens"),
                       "tokens_per_sec_video": tps,
                       "recall": recall(quotes, r["text"]) if r.get("ok") else None,
                       "cost": native_cost(mk, r) if r.get("ok") else None}
                runs.append({"rec": rec, "text": r.get("text", "")})
                print(f"  [{mk}] run{i+1} ok={r['ok']} chars={rec['chars']} "
                      f"vtok={r.get('video_tokens')} tok/s={tps} "
                      f"ptok={r.get('prompt_tokens')} err={str(r.get('error'))[:80]}",
                      file=sys.stderr)
            repro = None
            if len(runs) >= 2 and runs[0]["rec"]["ok"] and runs[1]["rec"]["ok"]:
                repro = recall(sample_quotes(runs[0]["text"]), runs[1]["text"])
            entry["native"][mk] = {"runs": [x["rec"] for x in runs],
                                   "reproducibility": repro}
        summary.append(entry)
        (results_dir / "native_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def print_native_table(summary: list[dict]):
    print("\n\n============ NATIVE LOW-RES (long clips) ============")
    for e in summary:
        c = e["clip"]
        print(f"\n{c['id']}  {c['duration_sec']}s  groq_ref={e['groq_ref_chars']}c")
        for mk, m in e["native"].items():
            for i, r in enumerate(m["runs"]):
                co = r.get("cost") or {}
                hr = c["duration_sec"] / 3600
                cph = round(co["computed_usd"] / hr, 3) if co.get("computed_usd") and hr else None
                print(f"  {mk} run{i+1}: ok={r['ok']} chars={r['chars']} "
                      f"(ref {e['groq_ref_chars']}) ptok={r.get('prompt_tokens')} "
                      f"tok/s={r.get('tokens_per_sec_video')} cost=${co.get('computed_usd')} "
                      f"(${cph}/hr) err={str(r.get('error'))[:50]}")
            rp = m.get("reproducibility")
            if rp:
                print(f"  {mk} reproducibility: exact={rp.get('exact_pct')}% "
                      f"fuzzy={rp.get('fuzzy_pct')}%")


# ---- driver ----------------------------------------------------------------
def run(clips: list[dict], models: list[str], results_dir: Path,
        gemini_runs: int, or_key: str):
    results_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for clip in clips:
        cid = clip["id"]
        url = clip["url"]
        dur_s = clip.get("duration_sec")
        print(f"\n=== {cid} | {clip.get('bucket')} | {url} ===", file=sys.stderr)

        entry = {"clip": clip, "groq": None, "gemini": {}}

        # Groq reference (guarded: a yt-dlp/ffmpeg failure on one clip must not
        # abort the whole matrix — record it and continue).
        print("  [groq] downloading + transcribing ...", file=sys.stderr)
        try:
            g = groq_transcribe(url)
        except Exception as e:
            g = {"ok": False, "text": "", "latency": None, "error": str(e)}
            print(f"  [groq] FAILED: {e}", file=sys.stderr)
        (results_dir / f"{cid}.groq.txt").write_text(g.get("text", ""))
        quotes = sample_quotes(g["text"]) if g["ok"] else []
        entry["groq"] = {"ok": g["ok"], "latency": g.get("latency"),
                         "error": g.get("error"),
                         "chars": len(g.get("text", "")), "n_quotes": len(quotes)}
        entry["quotes"] = [q[:160] for q in quotes]
        print(f"  [groq] ok={g['ok']} chars={len(g.get('text',''))} "
              f"quotes={len(quotes)}", file=sys.stderr)

        for mk in models:
            runs = []
            for i in range(gemini_runs):
                print(f"  [{mk}] run {i+1}/{gemini_runs} ...", file=sys.stderr)
                r = gemini_transcribe(url, mk, or_key)
                (results_dir / f"{cid}.{mk}.run{i+1}.txt").write_text(
                    r.get("text", "") if r.get("ok") else f"ERROR: {r.get('error')}")
                rec = {"ok": r["ok"], "status": r.get("status"),
                       "latency": r.get("latency"), "error": r.get("error"),
                       "chars": len(r.get("text", "")),
                       "recall": recall(quotes, r["text"]) if r.get("ok") else None,
                       "cost": cost_of(mk, r.get("usage", {})) if r.get("ok") else None,
                       "tokens_per_sec_video": (
                           round((r.get("usage", {}).get("prompt_tokens") or 0) / dur_s, 1)
                           if r.get("ok") and dur_s else None)}
                runs.append({"rec": rec, "text": r.get("text", "")})
                print(f"  [{mk}] run {i+1} ok={r['ok']} status={r.get('status')} "
                      f"err={str(r.get('error'))[:80]}", file=sys.stderr)

            # reproducibility: sample from run1, check survival in run2
            repro = None
            if len(runs) >= 2 and runs[0]["rec"]["ok"] and runs[1]["rec"]["ok"]:
                rq = sample_quotes(runs[0]["text"])
                repro = recall(rq, runs[1]["text"])
            entry["gemini"][mk] = {
                "runs": [x["rec"] for x in runs],
                "reproducibility": repro,
            }
        summary.append(entry)
        (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def print_table(summary: list[dict]):
    print("\n\n================ SUMMARY ================")
    for e in summary:
        c = e["clip"]
        print(f"\n{c['id']}  [{c.get('bucket')}]  {c.get('duration_sec')}s  {c['url']}")
        gq = e["groq"]
        print(f"  groq: ok={gq['ok']} chars={gq['chars']} quotes={gq['n_quotes']}")
        for mk, m in e["gemini"].items():
            for i, r in enumerate(m["runs"]):
                rc = r.get("recall") or {}
                co = r.get("cost") or {}
                print(f"  {mk} run{i+1}: ok={r['ok']} status={r.get('status')} "
                      f"chars={r['chars']} "
                      f"recall_exact={rc.get('exact_pct')}% fuzzy={rc.get('fuzzy_pct')}% "
                      f"tok/s={r.get('tokens_per_sec_video')} "
                      f"cost=${co.get('computed_usd')} err={str(r.get('error'))[:60]}")
            rp = m.get("reproducibility")
            if rp:
                print(f"  {mk} reproducibility (run1->run2): "
                      f"exact={rp.get('exact_pct')}% fuzzy={rp.get('fuzzy_pct')}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="1 short clip, flash only, 1 run — verify the OpenRouter schema")
    ap.add_argument("--native-long", action="store_true",
                    help="Phase 2: native SDK, forced low-res, LONG clips only")
    ap.add_argument("--native-smoke", action="store_true",
                    help="native SDK on 1 short clip — verify SDK call + that low-res is honored")
    ap.add_argument("--clips", default=str(Path(__file__).parent / "clips.json"))
    ap.add_argument("--results", default=os.environ.get(
        "EVAL_RESULTS_DIR",
        str(Path(tempfile.gettempdir()) / "gemini-eval-results")),
        help="where to write raw transcripts (kept OUT of the repo — copyright)")
    ap.add_argument("--models", default="flash,flash-lite")
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()

    clips = json.loads(Path(args.clips).read_text())
    clips = [c for c in clips if c.get("use", True)]
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    # --- Phase 2: native SDK low-res paths (need GEMINI_API_KEY, not OpenRouter) ---
    if args.native_smoke or args.native_long:
        gem_key = os.environ.get("GEMINI_API_KEY", "")
        if not gem_key:
            sys.exit("GEMINI_API_KEY not set (run: set -a && . ./.env && set +a)")
        if args.native_smoke:
            short = min(clips, key=lambda c: c.get("duration_sec", 1e9))
            print(f"NATIVE SMOKE: {short['id']} ({short['duration_sec']}s) flash low-res",
                  file=sys.stderr)
            r = gemini_transcribe_native(short["url"], "flash", gem_key)
            tps = (round((r.get("video_tokens") or 0) / short["duration_sec"], 1)
                   if r.get("ok") else None)
            print(json.dumps({
                "ok": r["ok"], "error": r.get("error"), "chars": len(r.get("text", "")),
                "prompt_tokens": r.get("prompt_tokens"),
                "video_tokens": r.get("video_tokens"), "audio_tokens": r.get("audio_tokens"),
                "video_tok_per_sec": tps,  # ~100 => low-res honored; ~295 => not
                "head": (r.get("text") or "")[:300]}, indent=2))
            return
        nmodels = [m.strip() for m in args.models.split(",")
                   if m.strip() in NATIVE_MODELS]
        summary = run_native_long(clips, Path(args.results), args.runs, gem_key,
                                  models=nmodels or None)
        print_native_table(summary)
        print(f"\nNative transcripts + native_summary.json -> {args.results}", file=sys.stderr)
        return

    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not or_key:
        sys.exit("OPENROUTER_API_KEY not set (run: set -a && . ./.env && set +a)")

    if args.smoke:
        short = min(clips, key=lambda c: c.get("duration_sec", 1e9))
        print(f"SMOKE: {short['id']} ({short.get('duration_sec')}s) via flash, 1 run",
              file=sys.stderr)
        r = gemini_transcribe(short["url"], "flash", or_key)
        print(json.dumps({"ok": r["ok"], "status": r.get("status"),
                          "error": r.get("error"),
                          "chars": len(r.get("text", "")),
                          "head": (r.get("text") or "")[:400],
                          "usage": r.get("usage")}, indent=2))
        return

    summary = run(clips, models, Path(args.results), args.runs, or_key)
    print_table(summary)
    print(f"\nRaw transcripts + summary.json -> {args.results}", file=sys.stderr)


if __name__ == "__main__":
    main()
