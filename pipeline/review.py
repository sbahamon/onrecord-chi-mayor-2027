"""Independent verification of extracted statements before they go live.

Belt and suspenders: a deterministic quote-in-transcript check the model can't
override, plus the reviewer model's judgment on faithfulness and attribution.
A statement is ``confirmed`` only if the quote is really there AND the model
finds the summary faithful AND attribution is correct.

One honest exception, and it is narrow (#92). For **audio** the reviewer
re-transcribes the source rather than re-fetching it, and does not get identical
text back — the same episode encodes differently on a different machine — so the
exact check fails on genuine quotes and every podcast row flagged. There the
reviewer is told the quote drifted, shown the nearest passage, and asked whether
it is the same statement. That verdict is reported as ``reconciled``, never
"verified", and carries the ratio and the passage so a human can judge it in
seconds. The deterministic check still stands alone for articles, and in
``extract`` it is never relaxed at all.

Auto-merge is gated behind an explicit config flag that ships OFF. Even a fully
confirmed, high-confidence batch will not auto-merge unless someone turns it on.
"""
from __future__ import annotations

import difflib
import re

from pipeline.extract import quote_in_transcript
from pipeline.ingest import AUDIO_TYPES

# How similar the nearest transcript passage must be before the reviewer is even
# asked about it. This is a LOCATE gate, not a truth gate: it decides "is this
# the same passage", never "does it say the same thing".
#
# A truth gate here is impossible, and that is measured, not assumed. Against the
# real giannoulias quote, a negated version ("not reduce ... could not choose")
# scores 0.979 while a genuine re-transcription scores 0.969 — inverting a
# position changes two short words out of forty-five, so the lie is *more*
# similar to the original than the truth is. Adding "numbers and negations must
# match" rejects that pair but waves through "will"->"could", "reduce"->"review"
# and an inserted "some", all above 0.94; a blocklist only ever catches the
# attacks someone thought of. So the meaning question goes to the reviewer model,
# which is the component already trusted to judge faithfulness and attribution.
#
# Deliberately a constant and not a config knob: a trust parameter that can be
# lowered without a test failing is a weakened guard.
FUZZY_LOCATE_MIN_RATIO = 0.90

# Extra tokens of context shown either side of the matched window (see
# best_matching_passage) — enough to carry a clause, small enough that the
# reviewer is still judging the passage and not the surrounding argument.
_PASSAGE_PAD_TOKENS = 8

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

# Appended only when the stored quote is not verbatim in *this* transcript and the
# source is audio. review.yml re-transcribes to verify and does not get identical
# text back — the runner encodes the audio differently — so the exact check fails
# on genuine quotes. Rather than guess with a threshold, tell the reviewer the
# quote drifted, show it the passage, and ask the one question that matters.
RECONCILE_ADDENDUM = (
    " NOTE: the quote was taken from a different transcription of the same audio, "
    "so it is not word-for-word in the transcript you are given. You are also shown "
    "the closest passage. Additionally decide: is that passage the SAME STATEMENT "
    "as the quote? Differences in wording, filler, punctuation or spelling are "
    "expected and fine. A difference in what is being claimed is not — check the "
    "numbers and quantities, the polarity (nothing negated or un-negated), and the "
    "modality (a promise is not a possibility). Add "
    '"same_statement": true|false to your JSON.'
)


def _fuzzy_tokens(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Normalized tokens plus each one's span in the ORIGINAL text.

    Apostrophes are removed rather than split on, so ``don't`` stays the single
    token ``dont`` instead of becoming ``don`` + ``t`` — otherwise a negation
    dissolves into noise. Hyphens become spaces so ``pre-approved`` and
    ``pre approved`` agree, which is exactly how transcriptions of the same audio
    differ. ``extract._normalize`` is deliberately NOT changed: the extraction
    guard matches a quote against the very transcript the model was handed, where
    an exact match should always hold.

    The spans let the caller hand the reviewer the real passage, punctuation and
    all, instead of a normalized reconstruction of it.
    """
    tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\S+", text):
        cleaned = m.group(0).replace("\u2019", "").replace("'", "").lower().replace("-", " ")
        for tok in re.sub(r"[^a-z0-9 ]", " ", cleaned).split():
            tokens.append(tok)
            spans.append((m.start(), m.end()))
    return tokens, spans


def best_matching_passage(quote: str, transcript: str) -> tuple[float, str]:
    """Find the transcript passage most like ``quote``; return (ratio, passage).

    A window the length of the quote slides over the transcript. Only the *best*
    window is returned — the caller decides what to do with it, and this function
    makes no claim about meaning.
    """
    q_tokens, _ = _fuzzy_tokens(quote)
    t_tokens, t_spans = _fuzzy_tokens(transcript)
    if not q_tokens or not t_tokens:
        return 0.0, ""

    width = len(q_tokens)
    # The quote is seq2 so difflib indexes it once and reuses that across every
    # window (set_seq1 is the cheap side); the quick_ratio pair are upper bounds,
    # so a window that cannot beat the best so far never pays for a full compare.
    matcher = difflib.SequenceMatcher(None)
    matcher.set_seq2(q_tokens)

    best_ratio, best_i, best_j = 0.0, 0, 0
    for i in range(max(1, len(t_tokens) - width + 1)):
        matcher.set_seq1(t_tokens[i:i + width])
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_i, best_j = ratio, i, min(i + width, len(t_tokens))

    if best_j == 0:
        return 0.0, ""
    # The ratio is measured on the window; the passage returned is padded. A
    # window is exactly quote-length, so any drift in token count (a duplicated
    # filler, a dropped article) walks its edges off the sentence — observed on
    # the real giannoulias quote, which came back ending "developers could",
    # one word short of "choose". The reviewer is being asked whether this is the
    # same statement, so it must see the whole thought; padding what is shown
    # never loosens what is measured.
    lo = max(0, best_i - _PASSAGE_PAD_TOKENS)
    hi = min(len(t_spans), best_j + _PASSAGE_PAD_TOKENS)
    return best_ratio, transcript[t_spans[lo][0]:t_spans[hi - 1][1]]


def verify_statement(statement: dict, transcript: str, *, llm, model: str,
                     media_type: str | None = None) -> dict:
    exact = quote_in_transcript(statement["quote"], transcript)
    mechanism = statement.get("mechanism")

    # Reconciliation is for audio only. An article is re-fetched, not
    # re-transcribed, so its text comes back the same and a near-miss there is a
    # real problem worth flagging rather than explaining away. Default None keeps
    # any caller that doesn't say strict.
    ratio: float | None = None
    passage = ""
    reconciling = False
    if not exact and media_type in AUDIO_TYPES:
        ratio, passage = best_matching_passage(statement["quote"], transcript)
        reconciling = ratio >= FUZZY_LOCATE_MIN_RATIO

    system = REVIEW_SYSTEM + (RECONCILE_ADDENDUM if reconciling else "")
    details = (
        f"Candidate: {statement['candidate']}\n"
        f"Stance: {statement['stance']}\n"
        f"Summary: {statement['summary']}\n"
        f"Quote: {statement['quote']}\n"
        f"Claimed mechanism: {mechanism if mechanism else '(none claimed)'}\n"
    )
    if reconciling:
        details += f"\nClosest passage in this transcript:\n{passage}\n"

    judgment = llm.complete_json(
        model=model,
        system=system,
        user=f"{details}\nTranscript:\n{transcript}",
    )
    faithful = bool(judgment.get("faithful"))
    attribution_ok = bool(judgment.get("attribution_ok"))
    # Only a CLAIMED mechanism can fail this. A null one means the candidate
    # offered no specifics, which is a finding rather than an error, and a
    # statement predating the migration has no key at all — neither should be
    # flagged for it. This check exists solely to stop the extractor inventing
    # specificity to satisfy the field.
    mechanism_supported = bool(judgment.get("mechanism_supported")) if mechanism else True
    # Only a reconciliation we actually asked about can answer this; an exact
    # match never needs it, and a passage below the locate gate was never shown.
    same_statement = bool(judgment.get("same_statement")) if reconciling else False
    quote_verified = exact or same_statement
    quote_match = "exact" if exact else ("reconciled" if quote_verified else "none")
    confirmed = quote_verified and faithful and attribution_ok and mechanism_supported

    return {
        "candidate": statement["candidate"],
        "topic": statement["topic"],
        "confidence": statement.get("confidence", 0.0),
        "quote_verified": quote_verified,
        "quote_match": quote_match,
        "quote_match_ratio": ratio,
        "matched_passage": passage if quote_match == "reconciled" else "",
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
        "quote_match": "none",
        "quote_match_ratio": None,
        "matched_passage": "",
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
        verify_statement(stmt, transcript, llm=llm, model=model,
                         media_type=evidence.get("media_type"))
        for stmt in evidence["statements"]
    ]


def decide_label(verdicts: list[dict]) -> str:
    """Three outcomes, because ``unverifiable`` and ``flagged`` are different findings.

    ``unverifiable`` (#69) means the source could not be re-fetched, so nothing was
    checked. ``flagged`` means something was checked and is wrong — the quote is not
    in the transcript, the attribution is off, or a claimed mechanism is absent. Only
    the second is a problem with the evidence.

    Collapsing them made the label useless in practice: PR #97 came back 18
    unverifiable / 0 contradicted, purely because two outlets 403 the runner, and
    still read ``ai-flagged``. This gets structurally worse, not better — backfills
    now run locally *because* outlets block datacenter IPs, so every locally sourced
    PR is guaranteed to come back partly unverifiable. An always-red label is one
    nobody reads, which is the lesson #92 already paid for.

    Note this changes only what humans are told. ``should_auto_merge`` is unchanged
    and still requires every verdict to be ``confirmed``.
    """
    if not verdicts:
        return "ai-flagged"
    if all(v["verdict"] == "confirmed" for v in verdicts):
        return "ai-verified"
    if any(v["verdict"] == "flagged" for v in verdicts):
        return "ai-flagged"
    return "ai-unverifiable"


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
    unverifiable = sum(1 for v in verdicts if v["verdict"] == "unverifiable")
    contradicted = sum(1 for v in verdicts if v["verdict"] == "flagged")
    lines = [
        "## 🤖 Automated verification",
        "",
        # Lead with the breakdown so the summary line answers "is there a problem?"
        # without opening the comment. "13/31 confirmed" alone reads as alarming
        # when the other 18 are just an outlet blocking the runner.
        f"**{confirmed} confirmed · {unverifiable} unverifiable · "
        f"{contradicted} contradicted** (of {len(verdicts)}). "
        "Human review still required — this is advisory.",
        "",
    ]
    if unverifiable and not contradicted:
        lines += [
            "_Nothing was contradicted. The unverifiable statements are sources the "
            "runner could not re-fetch (#69) — verify those by hand._",
            "",
        ]
    for v in verdicts:
        icon = "✅" if v["verdict"] == "confirmed" else "⚠️"
        if v["verdict"] == "unverifiable":
            # Never say the quote is missing here — no transcript was fetched
            # to look in, so that would assert something we did not check.
            quote_note = "**source could not be re-fetched; verify by hand**"
        elif v.get("quote_match") == "reconciled":
            # Never call this "verified" — it was not verbatim. Say what happened
            # and show the ratio, so a human can judge the claim in seconds
            # instead of trusting the label.
            quote_note = "quote **reconciled** against a re-transcription"
            if isinstance(v.get("quote_match_ratio"), (int, float)):
                quote_note += f" (ratio {v['quote_match_ratio']:.2f})"
            quote_note += "; not verbatim, reviewer judged it the same statement"
        elif v.get("quote_verified"):
            quote_note = "quote verified"
        else:
            quote_note = "**quote NOT found in transcript**"
            # A bare "not found" reads as "fabricated" and is what trained
            # everyone to ignore this label on audio rows. The nearest-passage
            # ratio distinguishes a re-transcription drift from an invention.
            if isinstance(v.get("quote_match_ratio"), (int, float)):
                quote_note += f" (nearest passage ratio {v['quote_match_ratio']:.2f})"
        if v["verdict"] == "flagged" and not v.get("mechanism_supported", True):
            quote_note += ", **claimed mechanism not found in transcript**"
        lines.append(
            f"- {icon} **{v['candidate']} / {v['topic']}** — {v['verdict']} "
            f"({quote_note}). {v.get('notes', '')}"
        )
        if v.get("quote_match") == "reconciled" and v.get("matched_passage"):
            lines.append(f"  - transcript passage: \u201c{v['matched_passage']}\u201d")
    return "\n".join(lines)
