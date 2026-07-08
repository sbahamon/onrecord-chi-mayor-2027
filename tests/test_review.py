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

    def complete_json(self, *, model, system, user):
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
