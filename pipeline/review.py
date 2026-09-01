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
    "candidate, the claimed stance/summary, the quote, and any claimed policy "
    "mechanism, decide: is the summary a faithful representation of what the "
    "candidate said (not overstated), is it correctly attributed to the candidate "
    "(not describing someone else's view or a hypothetical), and — when a "
    "mechanism is claimed — is that mechanism actually stated in the transcript "
    "rather than inferred, generalised, or invented? Respond as JSON: "
    '{"faithful": true|false, "attribution_ok": true|false, '
    '"mechanism_supported": true|false, "notes": "..."}.'
)


def verify_statement(statement: dict, transcript: str, *, llm, model: str) -> dict:
    quote_verified = quote_in_transcript(statement["quote"], transcript)
    mechanism = statement.get("mechanism")

    judgment = llm.complete_json(
        model=model,
        system=REVIEW_SYSTEM,
        user=(
            f"Candidate: {statement['candidate']}\n"
            f"Stance: {statement['stance']}\n"
            f"Summary: {statement['summary']}\n"
            f"Quote: {statement['quote']}\n"
            f"Claimed mechanism: {mechanism if mechanism else '(none claimed)'}\n\n"
            f"Transcript:\n{transcript}"
        ),
    )
    faithful = bool(judgment.get("faithful"))
    attribution_ok = bool(judgment.get("attribution_ok"))
    # Only a CLAIMED mechanism can fail this. A null one means the candidate
    # offered no specifics, which is a finding rather than an error, and a
    # statement predating the migration has no key at all — neither should be
    # flagged for it. This check exists solely to stop the extractor inventing
    # specificity to satisfy the field.
    mechanism_supported = bool(judgment.get("mechanism_supported")) if mechanism else True
    confirmed = quote_verified and faithful and attribution_ok and mechanism_supported

    return {
        "candidate": statement["candidate"],
        "topic": statement["topic"],
        "confidence": statement.get("confidence", 0.0),
        "quote_verified": quote_verified,
        "faithful": faithful,
        "attribution_ok": attribution_ok,
        "mechanism_supported": mechanism_supported,
        "verdict": "confirmed" if confirmed else "flagged",
        "notes": judgment.get("notes", ""),
    }


def _unverifiable(statement: dict, error: Exception) -> dict:
    """A verdict for a statement whose source could not be re-fetched.

    Deliberately not ``flagged``: flagged means the reviewer looked and had a
    concern, whereas here it never got to look. Only ``confirmed`` passes the
    label and auto-merge gates, so this is safe by construction either way.
    """
    return {
        "candidate": statement["candidate"],
        "topic": statement["topic"],
        "confidence": statement.get("confidence", 0.0),
        "quote_verified": False,
        "faithful": False,
        "attribution_ok": False,
        "mechanism_supported": False,
        "verdict": "unverifiable",
        "notes": f"source could not be re-fetched, so nothing was verified: {error}",
    }


def review_evidence(evidence: dict, *, llm, model: str, ingest_fn) -> list[dict]:
    """Re-ingest the evidence's source and verify each statement against it.

    Transcripts aren't stored in the repo (copyright), so the reviewer rebuilds
    the transcript from the original source at review time. ``ingest_fn`` is the
    ingestion callable (injected for tests).
    """
    source = {
        "url": evidence["url"],
        "outlet": evidence["outlet"],
        "media_type": evidence["media_type"],
        "title": evidence["title"],
        "published_date": evidence["published_date"],
    }
    try:
        transcript = ingest_fn(source).get("transcript", "")
    except Exception as e:
        # The source is unreachable from *this* egress (an outlet 406/429s it,
        # a page moved). Without a transcript there is nothing to verify — but
        # an unguarded raise would abort the whole review, discarding verdicts
        # for every other source in the PR. Degrade to "unverifiable" so a human
        # checks these by hand; they can never read as confirmed.
        return [_unverifiable(stmt, e) for stmt in evidence["statements"]]
    return [
        verify_statement(stmt, transcript, llm=llm, model=model)
        for stmt in evidence["statements"]
    ]


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
        if v["verdict"] == "unverifiable":
            # Never say the quote is missing here — no transcript was fetched
            # to look in, so that would assert something we did not check.
            quote_note = "**source could not be re-fetched; verify by hand**"
        elif v.get("quote_verified"):
            quote_note = "quote verified"
        else:
            quote_note = "**quote NOT found in transcript**"
        if v["verdict"] == "flagged" and not v.get("mechanism_supported", True):
            quote_note += ", **claimed mechanism not found in transcript**"
        lines.append(
            f"- {icon} **{v['candidate']} / {v['topic']}** — {v['verdict']} "
            f"({quote_note}). {v.get('notes', '')}"
        )
    return "\n".join(lines)
