"""Extraction turns a transcript into validated candidate statements.

The LLM is injected (a fake here) so these tests never hit the network. The
logic under test is everything *around* the model call: schema validation of
the model's output, the hard invariant that a quote must actually appear in the
transcript (drop fabricated quotes), housing-vs-other routing, and preserving
the model's attribution flag.
"""
import pytest

from pipeline.extract import extract, ExtractionError

TRANSCRIPT = """
Host: Welcome. Let's talk housing.
Candidate Doe: We should legalize apartments in every neighborhood.
Candidate Doe: My opponent wants to freeze all new construction, which is wrong.
Candidate Doe: On schools, I'll hire a thousand teachers.
"""


class FakeLLM:
    """Returns a scripted JSON payload regardless of prompt."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, *, model, system, user):
        self.calls.append({"model": model, "system": system, "user": user})
        return self.payload


def base_stmt(**over):
    stmt = {
        "candidate": "jane-doe",
        "topic": "zoning-reform",
        "stance": "supports",
        "summary": "Backs legalizing apartments citywide.",
        "quote": "We should legalize apartments in every neighborhood.",
        "locator": None,
        "confidence": 0.9,
        "is_housing": True,
        "attribution_flag": False,
    }
    stmt.update(over)
    return stmt


def run(statements):
    llm = FakeLLM({"statements": statements})
    return extract(
        TRANSCRIPT,
        candidates=["jane-doe"],
        topics=["zoning-reform", "tenant-protections", "schools"],
        llm=llm,
        model="fake-model",
    )


def test_extracts_a_valid_housing_statement():
    result = run([base_stmt()])
    assert len(result.housing) == 1
    assert result.housing[0]["topic"] == "zoning-reform"


def test_drops_statement_whose_quote_is_not_in_transcript():
    # A fabricated quote must never survive extraction.
    result = run([base_stmt(quote="I will abolish all zoning tomorrow, guaranteed.")])
    assert result.housing == []
    assert result.dropped == 1


def test_quote_match_is_whitespace_and_case_insensitive():
    result = run([base_stmt(quote="  we SHOULD legalize   apartments in every neighborhood.  ")])
    assert len(result.housing) == 1


def test_non_housing_statement_is_routed_to_other_not_housing():
    result = run([base_stmt(topic="schools", is_housing=False,
                            quote="On schools, I'll hire a thousand teachers.")])
    assert result.housing == []
    assert len(result.other) == 1


def test_attribution_flag_is_preserved():
    stmt = base_stmt(
        summary="Says opponent wants a construction freeze.",
        quote="My opponent wants to freeze all new construction, which is wrong.",
        attribution_flag=True,
    )
    result = run([stmt])
    assert result.housing[0]["attribution_flag"] is True


def test_malformed_model_output_raises():
    llm = FakeLLM({"statements": [{"candidate": "jane-doe"}]})  # missing required fields
    with pytest.raises(ExtractionError):
        extract(TRANSCRIPT, candidates=["jane-doe"], topics=["zoning-reform"],
                llm=llm, model="fake-model")


def test_missing_statements_key_raises():
    llm = FakeLLM({"nope": []})
    with pytest.raises(ExtractionError):
        extract(TRANSCRIPT, candidates=["jane-doe"], topics=["zoning-reform"],
                llm=llm, model="fake-model")


def test_unknown_candidate_is_dropped():
    result = run([base_stmt(candidate="not-a-candidate")])
    assert result.housing == []
    assert result.dropped == 1
