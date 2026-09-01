"""The AI reviewer independently checks each extracted statement.

It combines a *deterministic* quote-in-transcript check (so a model can't wave
through a fabricated quote) with the reviewer model's judgment on faithfulness
and attribution. Then it decides a PR label and — critically — whether an item
may auto-merge. Auto-merge must stay OFF while the config flag is false, no
matter how confident everything is.
"""
from pipeline import review

TRANSCRIPT = "Candidate: We must end apartment bans, full stop. That is my position."

STMT = {
    "candidate": "example-candidate-a", "topic": "zoning-reform",
    "stance": "supports", "summary": "Backs ending apartment bans.",
    "quote": "We must end apartment bans, full stop.", "locator": "12:00",
    "confidence": 0.95, "is_housing": True, "attribution_flag": False,
}


class FakeReviewer:
    def __init__(self, verdict):
        self.verdict = verdict
        self.seen_system = ""
        self.seen_user = ""

    def complete_json(self, *, model, system, user):
        self.seen_system = system
        self.seen_user = user
        return self.verdict


def good_model():
    return FakeReviewer({"faithful": True, "attribution_ok": True, "notes": "checks out"})


def test_confirmed_when_quote_present_and_model_approves():
    v = review.verify_statement(STMT, TRANSCRIPT, llm=good_model(), model="m")
    assert v["verdict"] == "confirmed"
    assert v["quote_verified"] is True


def test_flagged_when_quote_absent_even_if_model_approves():
    fabricated = dict(STMT, quote="I will bulldoze every single-family home.")
    v = review.verify_statement(fabricated, TRANSCRIPT, llm=good_model(), model="m")
    assert v["quote_verified"] is False
    assert v["verdict"] == "flagged"


def test_flagged_when_model_finds_unfaithful():
    llm = FakeReviewer({"faithful": False, "attribution_ok": True, "notes": "summary overstates"})
    v = review.verify_statement(STMT, TRANSCRIPT, llm=llm, model="m")
    assert v["verdict"] == "flagged"


def test_label_verified_only_when_all_confirmed():
    confirmed = {"verdict": "confirmed", "confidence": 0.9}
    flagged = {"verdict": "flagged", "confidence": 0.9}
    assert review.decide_label([confirmed, confirmed]) == "ai-verified"
    assert review.decide_label([confirmed, flagged]) == "ai-flagged"
    assert review.decide_label([]) == "ai-flagged"


def test_auto_merge_off_when_config_disabled_regardless_of_verdicts():
    all_confirmed = [{"verdict": "confirmed", "confidence": 0.99}]
    cfg = {"auto_merge_enabled": False, "auto_merge_min_confidence": 0.85}
    assert review.should_auto_merge(all_confirmed, cfg) is False


def test_auto_merge_on_when_enabled_all_confirmed_and_confident():
    verdicts = [{"verdict": "confirmed", "confidence": 0.9}]
    cfg = {"auto_merge_enabled": True, "auto_merge_min_confidence": 0.85}
    assert review.should_auto_merge(verdicts, cfg) is True


def test_auto_merge_off_when_enabled_but_low_confidence_or_flagged():
    cfg = {"auto_merge_enabled": True, "auto_merge_min_confidence": 0.85}
    assert review.should_auto_merge([{"verdict": "confirmed", "confidence": 0.5}], cfg) is False
    assert review.should_auto_merge([{"verdict": "flagged", "confidence": 0.99}], cfg) is False


def test_review_evidence_reingests_source_and_verifies_each_statement():
    # Transcripts aren't stored; the reviewer re-ingests the source to verify.
    evidence = {
        "id": "e1", "url": "https://x/y", "outlet": "Outlet",
        "media_type": "article", "title": "T", "published_date": "2026-07-06",
        "statements": [STMT, dict(STMT, quote="A quote never spoken.")],
    }
    calls = {}

    def fake_ingest(source):
        calls["source"] = source
        return {"transcript": TRANSCRIPT}

    verdicts = review.review_evidence(
        evidence, llm=good_model(), model="m", ingest_fn=fake_ingest
    )
    assert calls["source"]["url"] == "https://x/y"
    assert calls["source"]["media_type"] == "article"
    assert verdicts[0]["verdict"] == "confirmed"       # quote present
    assert verdicts[1]["quote_verified"] is False       # fabricated quote caught


def test_review_comment_lists_each_verdict():
    verdicts = [
        {"candidate": "example-candidate-a", "topic": "zoning-reform",
         "verdict": "confirmed", "quote_verified": True, "notes": "checks out"},
        {"candidate": "example-candidate-a", "topic": "adus",
         "verdict": "flagged", "quote_verified": False, "notes": "quote not found"},
    ]
    body = review.render_review_comment(verdicts)
    assert "zoning-reform" in body and "adus" in body
    assert "confirmed" in body.lower() and "flagged" in body.lower()


class Boom(RuntimeError):
    """Stands in for a 406/429 the reviewer's re-ingest can't get past."""


def raising_ingest(source):
    raise Boom(f"406 Client Error: Not Acceptable for url: {source['url']}")


EVIDENCE = {
    "url": "https://www.cbsnews.com/chicago/news/example/",
    "outlet": "CBS News Chicago", "media_type": "article",
    "title": "Example", "published_date": "2026-08-02",
    "statements": [STMT, dict(STMT, topic="affordable-housing-funding")],
}


def test_unverifiable_when_source_cannot_be_reingested():
    """A source the reviewer can't re-fetch must not kill the whole review.

    The reviewer re-ingests every URL to verify quotes. If that fetch fails
    (an outlet 406/429s this egress), an unguarded raise takes down the run —
    including verdicts for sources that would have verified.
    """
    verdicts = review.review_evidence(
        EVIDENCE, llm=good_model(), model="m", ingest_fn=raising_ingest
    )

    assert len(verdicts) == 2, "one verdict per statement, not an abort"
    for v in verdicts:
        assert v["verdict"] == "unverifiable"
        assert v["quote_verified"] is False
        assert "406" in v["notes"]


def test_unverifiable_never_verifies_and_never_auto_merges():
    verdicts = review.review_evidence(
        EVIDENCE, llm=good_model(), model="m", ingest_fn=raising_ingest
    )
    cfg = {"auto_merge_enabled": True, "auto_merge_min_confidence": 0.5}

    assert review.decide_label(verdicts) == "ai-flagged"
    assert review.should_auto_merge(verdicts, cfg) is False


def test_review_comment_distinguishes_unverifiable_from_a_missing_quote():
    """'quote NOT found' would be a lie — we never got a transcript to look in."""
    body = review.render_review_comment(
        review.review_evidence(
            EVIDENCE, llm=good_model(), model="m", ingest_fn=raising_ingest
        )
    )
    assert "quote NOT found" not in body
    assert "could not be re-fetched" in body


# --- mechanism verification -------------------------------------------------
# The guard against the extractor inventing specificity to satisfy the new field.
# This is why the design captures the mechanism rather than grading specificity:
# "is this mechanism stated in the transcript?" is checkable, a 1-3 grade is not.

def test_unsupported_mechanism_is_flagged():
    stmt = dict(STMT, mechanism="Cut permit review to 30 days")
    llm = FakeReviewer({"faithful": True, "attribution_ok": True,
                        "mechanism_supported": False,
                        "notes": "the transcript names no permit timeline"})
    v = review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is False
    assert v["verdict"] == "flagged", "an invented mechanism must not confirm"


def test_supported_mechanism_confirms():
    stmt = dict(STMT, mechanism="End apartment bans")
    llm = FakeReviewer({"faithful": True, "attribution_ok": True,
                        "mechanism_supported": True, "notes": "stated outright"})
    v = review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is True
    assert v["verdict"] == "confirmed"


def test_a_null_mechanism_skips_the_check_and_can_still_confirm():
    """Vague is not wrong — there is simply nothing to verify."""
    stmt = dict(STMT, mechanism=None)
    llm = FakeReviewer({"faithful": True, "attribution_ok": True, "notes": "ok"})
    v = review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is True
    assert v["verdict"] == "confirmed"


def test_a_statement_with_no_mechanism_key_behaves_like_null():
    """Pre-migration data has no key at all and must not be flagged for it."""
    llm = FakeReviewer({"faithful": True, "attribution_ok": True, "notes": "ok"})
    v = review.verify_statement(STMT, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is True
    assert v["verdict"] == "confirmed"


def test_the_claimed_mechanism_is_put_in_front_of_the_reviewer():
    """A check the model is never shown is not a check."""
    stmt = dict(STMT, mechanism="End apartment bans")
    llm = FakeReviewer({"faithful": True, "attribution_ok": True,
                        "mechanism_supported": True, "notes": "ok"})
    review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert "End apartment bans" in llm.seen_user
    assert "mechanism" in llm.seen_system.lower()


def test_unverifiable_verdicts_carry_the_mechanism_key():
    """Every verdict dict must have one shape, or render/label code branches."""
    verdicts = review.review_evidence(
        EVIDENCE, llm=good_model(), model="m", ingest_fn=raising_ingest
    )
    assert all(v["mechanism_supported"] is False for v in verdicts)


def test_review_comment_calls_out_an_invented_mechanism():
    v = [{"candidate": "c", "topic": "t", "confidence": 0.9,
          "quote_verified": True, "faithful": True, "attribution_ok": True,
          "mechanism_supported": False, "verdict": "flagged", "notes": "n"}]
    body = review.render_review_comment(v)
    assert "mechanism not found in transcript" in body


# --- reconciling a quote against a re-transcription (#92) ---------------------
#
# review.yml re-transcribes audio to verify, and does not get byte-identical
# text back: the runner's ffmpeg encodes the same source differently, so the
# exact check fails on genuine quotes and `ai-flagged` became the DEFAULT for
# every podcast row. A permanently-red label is one nobody reads.
#
# A similarity threshold cannot fix it. Measured on the real giannoulias quote,
# a negated version ("not reduce ... could not choose") scores 0.979 while the
# genuine re-transcription scores 0.969 — inverting a position changes two short
# words, so the lie is MORE similar than the truth. A "numbers and negations must
# match" gate rejects those two but waves through "will"->"could", "reduce"->
# "review", and an inserted "some", all measured above 0.94.
#
# So the ratio only LOCATES the passage; the reviewer model judges whether it is
# the same statement. No semantics live in this module.

SPOKEN = (
    "Host: Let's talk housing. "
    "Candidate: Let's reduce costly building code requirements that drive up the cost "
    "of building homes, including requiring buildings with three stories or more to have "
    "two separate stairways, establishing a series of of, you know, pre approved "
    "architectural plans for apartments and single family homes that developers could "
    "choose to get. Host: Thank you."
)

# What extraction stored, from a DIFFERENT transcription of the same audio.
STORED_QUOTE = (
    "Let's reduce costly building code requirements that drive up the cost of building "
    "homes, including requiring buildings with three stories or more to have two separate "
    "stairways, establishing a series of, you know, pre-approved architectural plans for "
    "apartments and single family homes that developers could choose."
)

DRIFTED = dict(STMT, topic="zoning-reform", quote=STORED_QUOTE,
               summary="Backs cutting building-code requirements.")


def reconciling_model(same_statement=True, **over):
    verdict = {"faithful": True, "attribution_ok": True,
               "same_statement": same_statement, "notes": "n"}
    verdict.update(over)
    return FakeReviewer(verdict)


def test_a_drifted_audio_quote_is_reconciled_and_can_confirm():
    llm = reconciling_model()
    v = review.verify_statement(DRIFTED, SPOKEN, llm=llm, model="m", media_type="podcast")

    assert v["quote_match"] == "reconciled"
    assert v["verdict"] == "confirmed"
    assert 0.90 <= v["quote_match_ratio"] < 1.0
    # The reviewer must be TOLD the quote drifted and shown the passage, not left
    # to notice; and it must be asked the meaning question explicitly.
    assert "could choose to get" in llm.seen_user, "the matched passage is put in front of it"
    assert "same_statement" in llm.seen_system


def test_a_negated_near_miss_is_flagged_even_though_it_locates():
    """The case a threshold can never catch: 0.979, higher than the true positive."""
    negated = dict(DRIFTED, quote=STORED_QUOTE.replace("Let's reduce", "Let's not reduce"))
    llm = reconciling_model(same_statement=False)
    v = review.verify_statement(negated, SPOKEN, llm=llm, model="m", media_type="podcast")

    assert v["quote_match"] == "none"
    assert v["quote_verified"] is False
    assert v["verdict"] == "flagged"


def test_reconciliation_does_not_bypass_the_attribution_check():
    """Fuzzy locating must not become a way around the model's other judgments."""
    llm = reconciling_model(attribution_ok=False)
    v = review.verify_statement(DRIFTED, SPOKEN, llm=llm, model="m", media_type="podcast")

    assert v["quote_match"] == "reconciled"   # it IS the same passage
    assert v["verdict"] == "flagged"          # but it is not the candidate's


def test_an_article_quote_is_never_reconciled():
    """Articles re-fetch deterministically, so a near-miss there is a real problem."""
    llm = reconciling_model()
    v = review.verify_statement(DRIFTED, SPOKEN, llm=llm, model="m", media_type="article")

    assert v["quote_match"] == "none"
    assert v["verdict"] == "flagged"
    assert "same_statement" not in llm.seen_system


def test_a_fabricated_quote_never_reaches_the_reviewer_for_reconciliation():
    """Below the locate gate there is no passage to argue about — don't ask."""
    fabricated = dict(DRIFTED, quote="I will bulldoze every single-family home in Chicago.")
    llm = reconciling_model()
    v = review.verify_statement(fabricated, SPOKEN, llm=llm, model="m", media_type="podcast")

    assert v["quote_match"] == "none"
    assert v["verdict"] == "flagged"
    assert "same_statement" not in llm.seen_system


def test_an_exact_match_still_reports_exact_and_asks_nothing_extra():
    """The common path must not change — and must not spend a reconciliation."""
    llm = reconciling_model()
    v = review.verify_statement(STMT, TRANSCRIPT, llm=llm, model="m", media_type="podcast")

    assert v["quote_match"] == "exact"
    assert v["quote_match_ratio"] is None
    assert v["verdict"] == "confirmed"
    assert "same_statement" not in llm.seen_system


def test_review_evidence_passes_the_media_type_through():
    """Without it every row would be treated as an article and never reconcile."""
    evidence = {
        "id": "e1", "url": "https://x/y", "outlet": "Outlet", "media_type": "podcast",
        "title": "T", "published_date": "2026-08-02", "statements": [DRIFTED],
    }
    verdicts = review.review_evidence(
        evidence, llm=reconciling_model(), model="m",
        ingest_fn=lambda s: {"transcript": SPOKEN},
    )
    assert verdicts[0]["quote_match"] == "reconciled"


def test_the_comment_says_reconciled_and_never_claims_verbatim():
    """A human must be able to see it was not an exact match, and check it fast."""
    body = review.render_review_comment([{
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "verdict": "confirmed", "quote_verified": True, "quote_match": "reconciled",
        "quote_match_ratio": 0.97, "matched_passage": "could choose to get",
        "notes": "same statement",
    }])
    assert "reconciled" in body.lower()
    assert "0.97" in body
    assert "could choose to get" in body, "the passage is shown so it can be eyeballed"
    assert "quote verified" not in body.lower(), "must not claim a verbatim match"


def test_the_locator_finds_a_drifted_passage_in_a_long_transcript():
    """Sanity + speed: a real episode is ~40k chars of transcript."""
    import time
    filler = "The host asks another question about city budgets and pensions. " * 600
    ratio, passage = review.best_matching_passage(STORED_QUOTE, filler + SPOKEN + filler)

    assert ratio >= 0.90
    assert "two separate stairways" in passage
    start = time.perf_counter()
    review.best_matching_passage(STORED_QUOTE, filler + SPOKEN + filler)
    assert time.perf_counter() - start < 5.0


def test_the_locator_rejects_an_unrelated_passage():
    ratio, _ = review.best_matching_passage(
        "I will bulldoze every single-family home in Chicago.", SPOKEN
    )
    assert ratio < 0.90


def test_the_passage_is_padded_so_it_is_not_clipped_mid_thought():
    """The window is quote-length, so drift pushes its edges off the sentence.

    Seen on the real giannoulias quote against the Sun-Times' own transcript: a
    duplicated "of" earlier in the passage shifted the window and it ended at
    "developers could", one word short of "choose". The reviewer is being asked
    whether the passage is the same statement — handing it a sentence cut off
    mid-clause weakens the one judgment this whole path depends on, and makes the
    human's eyeball check harder too. The ratio is still measured on the window;
    only what gets shown is padded.
    """
    _, passage = review.best_matching_passage(STORED_QUOTE, SPOKEN)
    assert "could choose to get" in passage, "the end of the sentence survives"
    assert "Let's reduce" in passage
