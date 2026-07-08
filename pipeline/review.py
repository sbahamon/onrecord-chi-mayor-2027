"""Independent verification of extracted statements before they go live.

Belt and suspenders: a deterministic quote-in-transcript check the model can't
override, plus the reviewer model's judgment on faithfulness and attribution.
A statement is ``confirmed`` only if the quote is really there AND the model
finds the summary faithful AND attribution is correct.

Auto-merge is gated behind an explicit config flag that ships OFF. Even a fully
confirmed, high-confidence batch will not auto-merge unless someone turns it on.
"""
from __future__ import annotations

from pipeline.extract import quote_in_transcript

REVIEW_SYSTEM = (
    "You verify a claim extracted from a transcript. Given the transcript, the "
    "candidate, the claimed stance/summary, and the quote, decide: is the summary "
    "a faithful representation of what the candidate said (not overstated), and is "
    "it correctly attributed to the candidate (not describing someone else's view "
    "or a hypothetical)? Respond as JSON: "
    '{"faithful": true|false, "attribution_ok": true|false, "notes": "..."}.'
)


def verify_statement(statement: dict, transcript: str, *, llm, model: str) -> dict:
    quote_verified = quote_in_transcript(statement["quote"], transcript)

    judgment = llm.complete_json(
        model=model,
        system=REVIEW_SYSTEM,
        user=(
            f"Candidate: {statement['candidate']}\n"
            f"Stance: {statement['stance']}\n"
            f"Summary: {statement['summary']}\n"
            f"Quote: {statement['quote']}\n\n"
            f"Transcript:\n{transcript}"
        ),
    )
    faithful = bool(judgment.get("faithful"))
    attribution_ok = bool(judgment.get("attribution_ok"))
    confirmed = quote_verified and faithful and attribution_ok

    return {
        "candidate": statement["candidate"],
        "topic": statement["topic"],
        "confidence": statement.get("confidence", 0.0),
        "quote_verified": quote_verified,
        "faithful": faithful,
        "attribution_ok": attribution_ok,
        "verdict": "confirmed" if confirmed else "flagged",
        "notes": judgment.get("notes", ""),
    }


def decide_label(verdicts: list[dict]) -> str:
    if verdicts and all(v["verdict"] == "confirmed" for v in verdicts):
        return "ai-verified"
    return "ai-flagged"


def should_auto_merge(verdicts: list[dict], config: dict) -> bool:
    if not config.get("auto_merge_enabled", False):
        return False
    if not verdicts:
        return False
    threshold = config.get("auto_merge_min_confidence", 1.0)
    return all(
        v["verdict"] == "confirmed" and v.get("confidence", 0.0) >= threshold
        for v in verdicts
    )


def render_review_comment(verdicts: list[dict]) -> str:
    confirmed = sum(1 for v in verdicts if v["verdict"] == "confirmed")
    lines = [
        "## 🤖 Automated verification",
        "",
        f"{confirmed}/{len(verdicts)} statements confirmed. "
        "Human review still required — this is advisory.",
        "",
    ]
    for v in verdicts:
        icon = "✅" if v["verdict"] == "confirmed" else "⚠️"
        quote_note = "quote verified" if v.get("quote_verified") else "**quote NOT found in transcript**"
        lines.append(
            f"- {icon} **{v['candidate']} / {v['topic']}** — {v['verdict']} "
            f"({quote_note}). {v.get('notes', '')}"
        )
    return "\n".join(lines)
