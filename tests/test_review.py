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
