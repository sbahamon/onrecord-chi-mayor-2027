"""The one-time migration that adds `mechanism` to already-committed statements.

It must ONLY add a field. Re-extracting would re-fetch (some outlets
intermittently block CI ranges) and is non-deterministic — the same article has
yielded 0, 2 and 3 statements on separate runs at temperature 0 — so it could
silently drop human-approved statements, or reorder them and invalidate the
citation indexes that point at them.
"""
import json

import pytest

from pipeline import schemas
from scripts import backfill_mechanism as bm


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def complete_json(self, *, model, system, user):
        self.seen.append(user)
        return self.answer


STMT = {
    "candidate": "example-candidate-a", "topic": "zoning-reform",
    "stance": "supports", "summary": "Would end apartment bans.",
    "quote": "I will end apartment bans.", "locator": None,
    "confidence": 0.9, "is_housing": True, "attribution_flag": False,
}


def _evidence(tmp_path, statements, ev_id="hit"):
    d = tmp_path / "media-hits" / "2026-08"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ev_id}.json"
    path.write_text(json.dumps({
        "id": ev_id, "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": statements,
    }, indent=2))
    return path


def _stance(tmp_path, citations, **over):
    d = tmp_path / "stances" / "example-candidate-a"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "zoning-reform.json"
    path.write_text(json.dumps({
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "s", "citations": citations,
        "updated_date": "2026-08-02", **over,
    }, indent=2))
    return path


def test_adds_mechanism_without_touching_anything_else(tmp_path):
    path = _evidence(tmp_path, [STMT])
    before = json.loads(path.read_text())

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    after = json.loads(path.read_text())
    assert after["statements"][0]["mechanism"] == "End apartment bans"
    for key in ("quote", "summary", "stance", "confidence", "attribution_flag", "topic"):
        assert after["statements"][0][key] == before["statements"][0][key]
    assert len(after["statements"]) == 1, "must not add or drop statements"


def test_records_null_when_no_mechanism_is_named(tmp_path):
    path = _evidence(tmp_path, [dict(STMT, quote="Chicago needs more housing.")])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    assert json.loads(path.read_text())["statements"][0]["mechanism"] is None


def test_statement_order_is_preserved(tmp_path):
    """Citations pin an index; reordering would silently repoint every one."""
    path = _evidence(tmp_path, [dict(STMT, quote="first quote"),
                                dict(STMT, quote="second quote", topic="adus")])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    after = json.loads(path.read_text())["statements"]
    assert [s["quote"] for s in after] == ["first quote", "second quote"]


def test_an_already_migrated_statement_is_not_re_queried(tmp_path):
    """Re-running must be free and idempotent, not a second paid pass."""
    _evidence(tmp_path, [dict(STMT, mechanism="End apartment bans")])
    llm = FakeLLM({"mechanism": "something else"})

    counts = bm.migrate(tmp_path, llm=llm, model="m")

    assert llm.seen == [], "must not re-ask about a statement it already has"
    assert counts["statements"] == 0


def test_stance_gets_the_mechanism_of_the_statement_it_cites(tmp_path):
    _evidence(tmp_path, [dict(STMT, quote="vague"), dict(STMT, quote="specific")])
    spath = _stance(tmp_path, ["hit#1"])

    answers = iter([{"mechanism": None}, {"mechanism": "End apartment bans"}])

    class Seq:
        def complete_json(self, *, model, system, user):
            return next(answers)

    bm.migrate(tmp_path, llm=Seq(), model="m")

    assert json.loads(spath.read_text())["mechanism"] == "End apartment bans"


def test_stance_citations_and_dates_are_untouched(tmp_path):
    """This migration changes exactly one field; it is not a re-proposal."""
    _evidence(tmp_path, [STMT])
    spath = _stance(tmp_path, ["other-hit#0", "hit#0"])
    before = json.loads(spath.read_text())

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    after = json.loads(spath.read_text())
    assert after["citations"] == before["citations"]
    assert after["updated_date"] == before["updated_date"]
    assert after["summary"] == before["summary"]


def test_a_dangling_citation_is_skipped_not_crashed(tmp_path):
    """Extraction non-determinism means an index can outlive its statement."""
    _evidence(tmp_path, [STMT])
    spath = _stance(tmp_path, ["hit#7"])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    assert "mechanism" not in json.loads(spath.read_text())


def test_output_is_schema_valid(tmp_path):
    path = _evidence(tmp_path, [STMT])
    spath = _stance(tmp_path, ["hit#0"])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    schemas.validate(json.loads(path.read_text()), "evidence")
    schemas.validate(json.loads(spath.read_text()), "stance")


def test_a_record_array_on_a_stance_survives(tmp_path):
    """`record` is officeholder history and must never be collateral damage."""
    _evidence(tmp_path, [STMT])
    rec = [{"action": "Bring Chicago Home", "outcome": "failed",
            "date": "2024-03-19", "citations": ["hit#0"]}]
    spath = _stance(tmp_path, ["hit#0"], record=rec)

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    assert json.loads(spath.read_text())["record"] == rec


def test_a_null_mechanism_is_written_onto_the_cell(tmp_path):
    """Absent and null are different states, and this is where they collapse.

    `stance.get("mechanism")` on a cell with no key returns None, which equals a
    null mechanism — so a naive equality skip leaves the cell unassessed forever.
    Vague cells would stay indistinguishable from un-migrated ones, which is
    precisely the distinction the whole field exists to make.
    """
    _evidence(tmp_path, [dict(STMT, quote="Chicago needs more housing.")])
    spath = _stance(tmp_path, ["hit#0"])
    assert "mechanism" not in json.loads(spath.read_text())

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    written = json.loads(spath.read_text())
    assert "mechanism" in written, "the cell must be marked assessed-and-vague"
    assert written["mechanism"] is None


def test_refreshing_an_already_correct_cell_rewrites_nothing(tmp_path):
    """Idempotence: a second run must not churn files or their mtimes."""
    _evidence(tmp_path, [dict(STMT, mechanism=None)])
    spath = _stance(tmp_path, ["hit#0"])
    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")
    first = spath.read_text()

    counts = bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    assert spath.read_text() == first
    assert counts["stances"] == 0, "nothing left to change on a second pass"
