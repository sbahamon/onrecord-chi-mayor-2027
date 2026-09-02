"""Assemble extraction results into the files + PR body a review sees.

* evidence record (media hit) — housing statements, schema-valid
* proposed stance cells — one per (candidate, topic), citing the best statement
* PR body — human-readable, with quotes and source links so review is fast
"""
import json

import pytest

from pipeline import propose, schemas

INGEST_DOC = {
    "id": "2026-07-06-example-podcast-doe",
    "url": "https://example.com/ep1",
    "outlet": "Example Podcast",
    "media_type": "podcast",
    "title": "Doe on housing",
    "published_date": "2026-07-06",
    "transcript": "…",
}

HOUSING = [
    {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Backs citywide apartment legalization.",
        "quote": "Legalize apartments everywhere.", "locator": "10:00",
        "confidence": 0.7, "is_housing": True, "attribution_flag": False,
    },
    {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Same topic, higher-confidence take.",
        "quote": "We must end apartment bans, full stop.", "locator": "12:00",
        "confidence": 0.95, "is_housing": True, "attribution_flag": False,
    },
]


def test_build_evidence_record_is_schema_valid_and_has_statements():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    schemas.validate(ev, "evidence")  # must not raise
    assert ev["id"] == INGEST_DOC["id"]
    assert ev["discovered_date"] == "2026-07-07"
    assert len(ev["statements"]) == 2


def test_stance_proposals_pick_highest_confidence_per_candidate_topic():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    stances = propose.propose_stance_updates(ev, today="2026-07-07")
    assert len(stances) == 1  # one (candidate, topic) pair
    s = stances[0]
    schemas.validate(s, "stance")
    # Cites the higher-confidence statement (index 1), not index 0.
    assert s["citations"] == ["2026-07-06-example-podcast-doe#1"]
    assert s["summary"] == "Same topic, higher-confidence take."


def test_pr_body_includes_quotes_candidates_and_source_link():
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    stances = propose.propose_stance_updates(ev, today="2026-07-07")
    body = propose.render_pr_body(ev, stances)
    assert "Example Podcast" in body
    assert "https://example.com/ep1" in body
    assert "end apartment bans" in body  # the winning quote
    assert "example-candidate-a" in body


def test_evidence_and_stance_write_to_expected_paths(tmp_path):
    ev = propose.build_evidence_record(INGEST_DOC, HOUSING, discovered_date="2026-07-07")
    ev_path = propose.write_evidence(ev, tmp_path)
    assert ev_path == tmp_path / "media-hits" / "2026-07" / f"{ev['id']}.json"
    assert json.loads(ev_path.read_text())["id"] == ev["id"]

    stance = propose.propose_stance_updates(ev, today="2026-07-07")[0]
    s_path = propose.write_stance(stance, tmp_path)
    assert s_path == tmp_path / "stances" / "example-candidate-a" / "zoning-reform.json"


def test_build_evidence_record_rejects_non_housing_statement():
    bad = [dict(HOUSING[0], is_housing=False)]
    with pytest.raises(ValueError):
        propose.build_evidence_record(INGEST_DOC, bad, discovered_date="2026-07-07")


def test_write_stance_refuses_path_traversal(tmp_path):
    # Last-line defense: even if a bad topic/candidate reaches write_stance, it
    # must never escape data_dir/stances/. Guards the file-path sink directly.
    bad = {
        "candidate": "example-candidate-a", "topic": "../../ledger",
        "stance": "supports", "summary": "x", "citations": ["e#0"],
        "updated_date": "2026-07-07",
    }
    with pytest.raises(ValueError):
        propose.write_stance(bad, tmp_path)
    assert not (tmp_path / "ledger.json").exists()


def test_write_stance_preserves_an_existing_record(tmp_path):
    # `record` is written by the per-candidate backfill; `write_stance` is called
    # by daily discovery. Since write_stance rewrites the cell wholesale, an
    # unguarded write would silently erase the backfilled record the first time
    # discovery proposed a new position for that candidate+topic — the same class
    # of quiet data loss as the fixed-branch PR clobber, surfacing weeks later.
    existing = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "old", "citations": ["e#0"],
        "updated_date": "2026-07-07",
        "record": [{
            "action": "Bring Chicago Home transfer-tax referendum",
            "outcome": "failed", "date": "2024-03-19", "citations": ["e#0"],
        }],
    }
    propose.write_stance(existing, tmp_path)

    # Discovery proposes a fresh position for the same cell; it carries no record.
    updated = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "mixed", "summary": "new", "citations": ["e#1"],
        "updated_date": "2026-08-19",
    }
    path = propose.write_stance(updated, tmp_path)

    written = json.loads(path.read_text())
    assert written["summary"] == "new"          # position updated
    assert written["stance"] == "mixed"
    assert written["record"] == existing["record"]  # record survived


def test_write_stance_lets_an_explicit_record_win(tmp_path):
    # Preserving must not make the field un-editable: a caller that deliberately
    # supplies `record` (the backfill) overwrites what is on disk.
    first = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "x", "citations": ["e#0"],
        "updated_date": "2026-07-07",
        "record": [{"action": "old", "outcome": "stalled", "citations": ["e#0"]}],
    }
    propose.write_stance(first, tmp_path)

    second = dict(first, record=[
        {"action": "new", "outcome": "enacted", "citations": ["e#1"]},
    ])
    path = propose.write_stance(second, tmp_path)

    assert json.loads(path.read_text())["record"] == second["record"]


def test_write_stance_unions_citations_with_an_existing_cell(tmp_path):
    """Two sources supporting one position must both stay cited.

    `propose_stance_updates` runs per evidence file and cites only that file's
    best statement, so a candidate+topic backed by several media hits is written
    one file at a time. Replacing `citations` on each write means the cell ends up
    citing whichever file happened to be processed last — the earlier source is
    silently dropped even though its evidence record is still committed. Found
    completing the giannoulias backfill (#70): a WTTW citation already merged to
    main disappeared the moment the CBS row was processed.
    """
    first = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "from wttw", "citations": ["wttw-hit#0"],
        "updated_date": "2026-08-02",
    }
    propose.write_stance(first, tmp_path)

    second = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "from cbs", "citations": ["cbs-hit#0"],
        "updated_date": "2026-09-01",
    }
    path = propose.write_stance(second, tmp_path)

    written = json.loads(path.read_text())
    assert written["citations"] == ["wttw-hit#0", "cbs-hit#0"], "both sources cited, oldest first"
    # The newest proposal still owns the position itself.
    assert written["summary"] == "from cbs"
    assert written["updated_date"] == "2026-09-01"


def test_write_stance_does_not_duplicate_a_citation_on_re_run(tmp_path):
    """Re-running a row is routine (~$0.0006); it must be idempotent."""
    stance = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "s", "citations": ["wttw-hit#0"],
        "updated_date": "2026-08-02",
    }
    propose.write_stance(stance, tmp_path)
    path = propose.write_stance(dict(stance, updated_date="2026-09-01"), tmp_path)

    assert json.loads(path.read_text())["citations"] == ["wttw-hit#0"]


def test_write_stance_replaces_a_citation_from_the_same_evidence_file(tmp_path):
    """Union across sources, replace within one — or a re-run can dangle.

    Extraction is not reproducible run-to-run (observed live: the same article
    yielded 0, 2 and 3 statements on separate runs at temperature 0), and a
    citation pins a statement *index*. Accumulating indexes from the same
    evidence id would keep `hit#2` after a re-run that produced only two
    statements, and a dangling citation fails the data-integrity tests.
    """
    propose.write_stance({
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "first pass", "citations": ["cbs-hit#2"],
        "updated_date": "2026-08-02",
    }, tmp_path)

    # A re-run of the same source now finds its best statement at index 0.
    path = propose.write_stance({
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "re-run", "citations": ["cbs-hit#0"],
        "updated_date": "2026-09-01",
    }, tmp_path)

    assert json.loads(path.read_text())["citations"] == ["cbs-hit#0"], \
        "the stale index from the same evidence file must not survive"


# --- mechanism-first stance selection ---------------------------------------

def _evidence(statements):
    return {
        "id": "hit", "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": statements,
    }


def _stmt(**over):
    base = {"candidate": "example-candidate-a", "topic": "zoning-reform",
            "stance": "supports", "summary": "s", "quote": "q",
            "confidence": 0.9, "is_housing": True, "attribution_flag": False}
    return {**base, **over}


def test_stance_prefers_a_mechanism_over_higher_confidence():
    """A confident platitude must not outrank a concrete proposal.

    Without this the matrix stays vague even once the data improves: the
    extractor is reliably confident about "we need more housing".
    """
    stances = propose.propose_stance_updates(_evidence([
        _stmt(summary="Wants more housing.", quote="We need more housing.",
              confidence=0.95, mechanism=None),
        _stmt(summary="Would end apartment bans.", quote="I will end apartment bans.",
              confidence=0.70, mechanism="End apartment bans"),
    ]), today="2026-09-01")

    assert len(stances) == 1
    assert stances[0]["mechanism"] == "End apartment bans"
    assert stances[0]["citations"] == ["hit#1"], "cites the specific statement"
    assert stances[0]["summary"] == "Would end apartment bans."


def test_confidence_still_decides_between_two_mechanisms():
    stances = propose.propose_stance_updates(_evidence([
        _stmt(summary="A.", quote="qa", confidence=0.60,
              mechanism="Upzone transit corridors"),
        _stmt(summary="B.", quote="qb", confidence=0.90,
              mechanism="End apartment bans"),
    ]), today="2026-09-01")

    assert stances[0]["mechanism"] == "End apartment bans"


def test_a_null_mechanism_still_produces_a_cell_carrying_null():
    """Record and mark, never drop — the vague marker has to reach the site."""
    stances = propose.propose_stance_updates(_evidence([
        _stmt(mechanism=None),
    ]), today="2026-09-01")

    assert len(stances) == 1
    assert stances[0]["mechanism"] is None


def test_a_statement_with_no_mechanism_key_produces_a_cell_without_one():
    """Pre-migration data must not gain a spurious null.

    Absent means "not assessed"; null means "assessed, none offered". Copying a
    key that isn't there would mark every un-migrated cell as vague.
    """
    stances = propose.propose_stance_updates(_evidence([_stmt()]), today="2026-09-01")

    assert "mechanism" not in stances[0]


# --- a weaker source must not quietly make a specific cell vague (#90) ---------

def _cell(**over):
    base = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "specific take",
        "citations": ["podcast-hit#0"], "updated_date": "2026-08-02",
        "mechanism": "cut permit review to 30 days",
    }
    base.update(over)
    return base


def test_write_stance_does_not_downgrade_a_specific_cell_to_vague(tmp_path):
    """A mechanism-less proposal must not overwrite a mechanism-bearing cell (#90).

    `citations` union across evidence files (#70/#72) but `stance`, `summary` and
    `mechanism` came from whichever file was written last — and processing order
    is arbitrary (alphabetical in a backfill, discovery order in the cron). So a
    weaker source silently replaced a stronger one.

    Seen live completing the giannoulias backfill: a Sun-Times row processed after
    a policy interview replaced *"Supports a millionaire's tax…"* +
    `mechanism: "3% tax on incomes above a million dollars"` with *"Will advocate
    for more funding for Chicago Public Schools"* + `mechanism: null` — while still
    citing the podcast statement that named the mechanism. The cell then told a
    reader the opposite of what its own evidence said, and nothing failed: schemas
    passed, citations resolved, the integrity tests were green.
    """
    propose.write_stance(_cell(), tmp_path)

    path = propose.write_stance(_cell(
        summary="vague take", stance="mixed", citations=["article-hit#0"],
        updated_date="2026-09-01", mechanism=None,
    ), tmp_path)

    written = json.loads(path.read_text())
    assert written["mechanism"] == "cut permit review to 30 days"
    # stance and summary travel WITH the mechanism — they describe one statement.
    # Keeping the summary while taking the new stance would caption support with
    # "mixed", which is worse than either source alone.
    assert written["summary"] == "specific take"
    assert written["stance"] == "supports"
    # The weaker source is still evidence for the position, so it is still cited,
    # and the cell did change — its date advances.
    assert written["citations"] == ["podcast-hit#0", "article-hit#0"]
    assert written["updated_date"] == "2026-09-01"


def test_write_stance_does_not_downgrade_when_the_proposal_omits_mechanism(tmp_path):
    """An absent key means "not yet assessed" — even weaker grounds to overwrite.

    Null at least says someone looked and found no specifics. Absent says nobody
    looked, so it must never replace a mechanism somebody did find.
    """
    propose.write_stance(_cell(), tmp_path)

    unassessed = _cell(summary="pre-migration take", citations=["old-hit#0"],
                       updated_date="2026-09-01")
    del unassessed["mechanism"]
    path = propose.write_stance(unassessed, tmp_path)

    written = json.loads(path.read_text())
    assert written["mechanism"] == "cut permit review to 30 days"
    assert written["summary"] == "specific take"


def test_write_stance_accepts_an_upgrade_from_vague_to_specific(tmp_path):
    """The guard is one-way: better evidence must still be able to win.

    Otherwise the first source to touch a cell would own it forever, and the
    matrix could never improve as sourcing improved — the failure the mechanism
    ranking in `_rank` exists to prevent, one layer up.
    """
    propose.write_stance(_cell(summary="vague take", mechanism=None), tmp_path)

    path = propose.write_stance(_cell(
        summary="specific take", citations=["podcast-hit#0"],
        updated_date="2026-09-01",
    ), tmp_path)

    written = json.loads(path.read_text())
    assert written["mechanism"] == "cut permit review to 30 days"
    assert written["summary"] == "specific take"


def test_write_stance_lets_a_newer_mechanism_replace_an_older_one(tmp_path):
    """Two specific sources stay last-write-wins, deliberately.

    Ranking *which* of two named mechanisms is better is a judgment this code
    cannot make, so the documented "newest proposal owns the position" behaviour
    is left intact. Only the downgrade to vague is blocked.
    """
    propose.write_stance(_cell(), tmp_path)

    path = propose.write_stance(_cell(
        summary="newer specific take", citations=["newer-hit#0"],
        updated_date="2026-09-01", mechanism="end single-family-only zoning",
    ), tmp_path)

    written = json.loads(path.read_text())
    assert written["mechanism"] == "end single-family-only zoning"
    assert written["summary"] == "newer specific take"


def _polarity_cell(stance, summary, citations, mechanism=None, topic="affordable-housing-funding"):
    cell = {
        "candidate": "example-candidate-a", "topic": topic,
        "stance": stance, "summary": summary, "citations": citations,
        "updated_date": "2026-09-01",
    }
    if mechanism is not None:
        cell["mechanism"] = mechanism
    return cell


def test_write_stance_refuses_to_invert_a_cells_polarity(tmp_path):
    # Found live backfilling matthew-brewer (#57). A Sun-Times piece about the CHA
    # suing HUD over anti-DEI grant *conditions* extracted as
    # topic=affordable-housing-funding, stance=opposes, confidence 1.0 — and since
    # it outranked the existing 0.95 statement and carried no mechanism (so the #90
    # guard did not apply), it took the cell. The public matrix then read
    # "opposes affordable-housing-funding" for a candidate who supports it.
    # Nothing failed: schemas passed, citations resolved, integrity stayed green.
    propose.write_stance(_polarity_cell("supports", "supports funding", ["a#0"]), tmp_path)

    path = propose.write_stance(
        _polarity_cell("opposes", "opposes HUD's grant conditions", ["b#0"]), tmp_path)

    written = json.loads(path.read_text())
    assert written["stance"] == "supports"            # polarity preserved
    assert written["summary"] == "supports funding"   # caption moves with it
    assert written["citations"] == ["a#0", "b#0"]     # evidence still accumulates


def test_write_stance_refuses_to_invert_from_opposes_to_supports(tmp_path):
    # The guard is symmetric: manufacturing support is as bad as manufacturing
    # opposition.
    propose.write_stance(_polarity_cell("opposes", "opposes it", ["a#0"]), tmp_path)
    path = propose.write_stance(_polarity_cell("supports", "supports it", ["b#0"]), tmp_path)
    assert json.loads(path.read_text())["stance"] == "opposes"


def test_write_stance_allows_a_first_write_of_any_polarity(tmp_path):
    # No existing cell means nothing to invert — a genuine `opposes` must land.
    path = propose.write_stance(_polarity_cell("opposes", "opposes it", ["a#0"]), tmp_path)
    assert json.loads(path.read_text())["stance"] == "opposes"


def test_write_stance_allows_softening_to_mixed(tmp_path):
    # `mixed` and `no-position` are not the opposite pole; adding nuance is exactly
    # what a second source should be able to do, and an existing test already pins
    # supports -> mixed. Only a polarity flip is refused.
    propose.write_stance(_polarity_cell("supports", "supports it", ["a#0"]), tmp_path)
    path = propose.write_stance(_polarity_cell("mixed", "it is complicated", ["b#0"]), tmp_path)
    written = json.loads(path.read_text())
    assert written["stance"] == "mixed"
    assert written["summary"] == "it is complicated"


def test_write_stance_polarity_guard_keeps_an_existing_mechanism(tmp_path):
    # The three position fields move as a unit, as they do for the #90 guard:
    # keeping a mechanism while taking an inverted stance would caption a named
    # instrument with the opposite position.
    propose.write_stance(
        _polarity_cell("supports", "supports a 3% rate", ["a#0"], mechanism="3% transfer tax"),
        tmp_path)
    path = propose.write_stance(_polarity_cell("opposes", "opposes conditions", ["b#0"]), tmp_path)
    written = json.loads(path.read_text())
    assert written["stance"] == "supports"
    assert written["mechanism"] == "3% transfer tax"


def test_write_stance_polarity_guard_drops_an_inverted_proposals_mechanism(tmp_path):
    # If the refused proposal's mechanism survived, the cell would caption the
    # preserved "supports" with an instrument the candidate named while opposing —
    # the exact stance/mechanism mismatch the guard exists to prevent. And because
    # the cell would then *have* a mechanism, the #90 guard would protect that
    # mismatch against any better mechanism-less proposal.
    propose.write_stance(_polarity_cell("supports", "supports funding", ["a#0"]), tmp_path)
    path = propose.write_stance(
        _polarity_cell("opposes", "opposes conditions", ["b#0"], mechanism="anti-DEI conditions"),
        tmp_path)
    written = json.loads(path.read_text())
    assert written["stance"] == "supports"
    # absent, not null: the existing cell was never assessed for a mechanism, and
    # null would claim it was and found none.
    assert "mechanism" not in written
