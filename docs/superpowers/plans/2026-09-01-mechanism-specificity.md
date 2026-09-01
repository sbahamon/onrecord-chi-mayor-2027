# Mechanism Specificity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tracker distinguish a candidate who names a policy mechanism from one who only voices support, by capturing the mechanism itself on every statement and stance cell.

**Architecture:** A new optional `mechanism: string | null` field flows from the extractor (which must find it in the quote), through the reviewer (which verifies it is actually stated), into stance-cell selection (which prefers a statement that has one). A one-time migration backfills the field over already-committed quotes without re-extracting. The site then renders the difference.

**Tech Stack:** Python 3.12, pytest, JSON Schema (draft 2020-12), OpenRouter via `pipeline/llm.py`, Astro + `node --test` for the site.

**Spec:** [`docs/superpowers/specs/2026-09-01-mechanism-specificity-design.md`](../specs/2026-09-01-mechanism-specificity-design.md)

## Global Constraints

- **TDD, always.** Every task writes a failing test first and watches it fail. `.venv/bin/pytest` runs offline on fixtures — no network, no keys.
- **`mechanism` is OPTIONAL in both schemas.** Both are `additionalProperties: false`, and ~50 committed statements plus 25 stance cells would become schema-invalid (failing CI) if it were required.
- **Three distinct states:** a string (mechanism named), `null` (assessed, none offered), absent (not yet assessed). Absent must never render as vague — Johnson's nine cells are specific, and mislabelling them inverts the finding.
- **The `stance` enum does not change.** `stance` answers *what*, `mechanism` answers *how*.
- **Borderline resolves to `null`.** Inventing specificity is the failure that matters; missing some is acceptable.
- **Never hand-write data files.** All `data/**` changes come from the CLI or the migration script.
- **Definition of a mechanism, used verbatim in prompts:** a program, rule change, funding source, quantity, or deadline, supported by the statement's `quote`.

---

### Task 1: Add the optional `mechanism` field to both schemas

**Files:**
- Modify: `schemas/evidence.schema.json` (`$defs.statement.properties`)
- Modify: `schemas/stance.schema.json` (`properties`)
- Test: `tests/test_schemas.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `mechanism` accepted as `{"type": ["string", "null"]}` on both `statement` and `stance` records; absence still validates.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schemas.py`:

```python
def test_statement_accepts_a_mechanism_string_null_or_absent():
    """Three distinct states; absent must stay valid or ~50 committed files break."""
    base = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "s", "quote": "q",
        "confidence": 0.9, "is_housing": True, "attribution_flag": False,
    }
    schemas.validate({**base, "mechanism": "Legalize ADUs citywide"}, "statement")
    schemas.validate({**base, "mechanism": None}, "statement")
    schemas.validate(base, "statement")  # absent


def test_stance_accepts_a_mechanism_string_null_or_absent():
    base = {
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "s", "citations": ["e#0"],
        "updated_date": "2026-09-01",
    }
    schemas.validate({**base, "mechanism": "Legalize ADUs citywide"}, "stance")
    schemas.validate({**base, "mechanism": None}, "stance")
    schemas.validate(base, "stance")  # absent
```

If `tests/test_schemas.py` has no `statement` validation helper, validate a full evidence
record containing the statement instead — check the file's existing patterns first and
follow them rather than inventing a new entry point.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_schemas.py -k mechanism -v`
Expected: FAIL — `additionalProperties` rejects `mechanism`.

- [ ] **Step 3: Add the property to both schemas**

In `schemas/evidence.schema.json`, inside `$defs.statement.properties`, after `attribution_flag`:

```json
"mechanism": {
  "description": "The concrete instrument the candidate named — a program, rule change, funding source, quantity, or deadline — supported by `quote`. null when they expressed only a goal, value, or direction. Absent means not yet assessed, which is NOT the same as null.",
  "type": ["string", "null"]
}
```

In `schemas/stance.schema.json`, inside `properties`, after `updated_date`:

```json
"mechanism": {
  "description": "Denormalized from the cited statement, like `summary`. null when the cited statement named no mechanism; absent when not yet assessed.",
  "type": ["string", "null"]
}
```

Add `mechanism` to neither `required` array.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass, including `tests/test_data_integrity.py` — existing data has no
`mechanism` and must still validate.

- [ ] **Step 5: Commit**

```bash
git add schemas/ tests/test_schemas.py
git commit -m "schemas: accept an optional mechanism on statements and stances"
```

---

### Task 2: Teach the extractor to capture the mechanism

**Files:**
- Modify: `pipeline/extract.py` (`SYSTEM_PROMPT`)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: the schema from Task 1.
- Produces: extracted statements may carry `mechanism`; `extract()` passes it through unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extract.py`, following the existing `FakeLLM` pattern in that file:

```python
def test_mechanism_is_passed_through_to_the_statement():
    """The field must survive extraction; the schema drop-path must not eat it."""
    payload = {"statements": [{
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Would legalize ADUs citywide.",
        "quote": "I will legalize accessory dwelling units in every ward.",
        "locator": None, "confidence": 0.9, "is_housing": True,
        "attribution_flag": False,
        "mechanism": "Legalize accessory dwelling units in every ward",
    }]}
    result = extract.extract(
        "I will legalize accessory dwelling units in every ward.",
        candidates=["example-candidate-a"], topics=["zoning-reform"],
        llm=FakeLLM(payload), model="m",
    )
    assert result.housing[0]["mechanism"] == "Legalize accessory dwelling units in every ward"


def test_a_statement_without_a_mechanism_still_extracts():
    """Vague statements are recorded and marked, never dropped."""
    payload = {"statements": [{
        "candidate": "example-candidate-a", "topic": "zoning-reform",
        "stance": "supports", "summary": "Supports more housing.",
        "quote": "Chicago needs more housing.",
        "locator": None, "confidence": 0.9, "is_housing": True,
        "attribution_flag": False, "mechanism": None,
    }]}
    result = extract.extract(
        "Chicago needs more housing.",
        candidates=["example-candidate-a"], topics=["zoning-reform"],
        llm=FakeLLM(payload), model="m",
    )
    assert len(result.housing) == 1
    assert result.housing[0]["mechanism"] is None
```

Check `tests/test_extract.py` for the exact `extract.extract(...)` signature and fake-LLM
class name in use, and match them — do not invent a different calling convention.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_extract.py -k mechanism -v`
Expected: FAIL — before Task 1 lands, schema validation drops the statement; after Task 1,
this should pass, which is why Task 1 comes first. If it already passes, the remaining work
in this task is the prompt only, which no unit test can assert — proceed to Step 3 and rely
on the live check in Step 4.

- [ ] **Step 3: Update `SYSTEM_PROMPT`**

Replace the `SYSTEM_PROMPT` assignment in `pipeline/extract.py` with:

```python
SYSTEM_PROMPT = (
    "You extract Chicago mayoral candidates' policy positions from a transcript. "
    "Return JSON: {\"statements\": [...]}. Each statement has candidate (slug), "
    "topic (slug), stance (supports|supports-with-conditions|opposes|mixed|"
    "no-position), summary, quote (VERBATIM from the transcript), locator "
    "(timestamp/paragraph or null), confidence (0-1), is_housing (bool), "
    "attribution_flag (true if the candidate is describing someone ELSE's "
    "position or speaking hypothetically rather than stating their own view), "
    "and mechanism. "
    "mechanism is the concrete instrument the candidate named: a program, a rule "
    "change, a funding source, a quantity, or a deadline. It MUST be supported by "
    "the quote you return. Use null when they expressed only a goal, a value, or a "
    "direction without saying how — 'housing should be affordable', 'build more "
    "housing', 'ensure residents aren't displaced' are all null. Directional "
    "phrasing counts ONLY if the quote names what would change: 'streamline "
    "permitting' is null, 'cut permit review to 30 days' is a mechanism. "
    "When in doubt use null; never invent specificity the quote does not contain. "
    "Quote candidates exactly; never paraphrase inside quote."
)
```

- [ ] **Step 4: Run the tests, then verify on a real source**

Run: `.venv/bin/pytest -q`
Expected: all pass.

Then confirm the prompt actually produces the field (no unit test can — it is model
behaviour). Against a scratch dir so the repo is untouched:

```bash
set -a && . ./.env && set +a
mkdir -p /tmp/mechcheck && cp -R data/registry /tmp/mechcheck/
.venv/bin/python -m pipeline --data-dir /tmp/mechcheck ingest-url \
  --url https://news.wttw.com/2026/08/02/backed-trade-unions-alexi-giannoulias-launches-campaign-chicago-mayor \
  --type article
grep -o '"mechanism": [^,]*' /tmp/mechcheck/media-hits/*/*.json
```

Expected: `"mechanism": null` for this source — Giannoulias's launch coverage contains no
mechanism, which is the whole reason this feature exists. Seeing `null` here is the
prompt working, not failing. Costs ~$0.0006.

- [ ] **Step 5: Commit**

```bash
git add pipeline/extract.py tests/test_extract.py
git commit -m "extract: capture the policy mechanism a candidate named, or null"
```

---

### Task 3: Make the reviewer verify the claimed mechanism

**Files:**
- Modify: `pipeline/review.py` (`REVIEW_SYSTEM`, `verify_statement`, `_unverifiable`, `render_review_comment`)
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: statements carrying `mechanism` (Task 2).
- Produces: each verdict dict gains `mechanism_supported: bool`. A statement is
  `confirmed` only if `quote_verified and faithful and attribution_ok and
  mechanism_supported`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review.py` (it already defines `TRANSCRIPT`, `STMT`, `FakeReviewer`,
`good_model`):

```python
def test_unsupported_mechanism_is_flagged():
    """The guard against the extractor inventing specificity to fill the field."""
    stmt = dict(STMT, mechanism="Cut permit review to 30 days")
    llm = FakeReviewer({"faithful": True, "attribution_ok": True,
                        "mechanism_supported": False,
                        "notes": "the transcript names no permit timeline"})
    v = review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is False
    assert v["verdict"] == "flagged"


def test_supported_mechanism_confirms():
    stmt = dict(STMT, mechanism="End apartment bans")
    llm = FakeReviewer({"faithful": True, "attribution_ok": True,
                        "mechanism_supported": True, "notes": "stated outright"})
    v = review.verify_statement(stmt, TRANSCRIPT, llm=llm, model="m")

    assert v["mechanism_supported"] is True
    assert v["verdict"] == "confirmed"


def test_a_null_mechanism_skips_the_check_and_can_still_confirm():
    """Vague is not wrong. There is simply nothing to verify."""
    stmt = dict(STMT, mechanism=None)
    # The model does not answer mechanism_supported at all for a null mechanism.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_review.py -k mechanism -v`
Expected: FAIL with `KeyError: 'mechanism_supported'`.

- [ ] **Step 3: Implement**

Replace `REVIEW_SYSTEM` in `pipeline/review.py` with:

```python
REVIEW_SYSTEM = (
    "You verify a claim extracted from a transcript. Given the transcript, the "
    "candidate, the claimed stance/summary, the quote, and any claimed policy "
    "mechanism, decide: is the summary a faithful representation of what the "
    "candidate said (not overstated), is it correctly attributed to the candidate "
    "(not describing someone else's view or a hypothetical), and — when a "
    "mechanism is claimed — is that mechanism actually stated in the transcript "
    "rather than inferred or invented? Respond as JSON: "
    '{"faithful": true|false, "attribution_ok": true|false, '
    '"mechanism_supported": true|false, "notes": "..."}.'
)
```

Replace `verify_statement` with:

```python
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
    # No mechanism claimed means nothing to verify — vague is not wrong, and
    # pre-migration statements have no key at all. Only a CLAIMED mechanism can
    # fail this check, which is what stops the extractor inventing specificity.
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
```

In `_unverifiable`, add `"mechanism_supported": False,` alongside the other `False` fields
so every verdict dict has the same shape.

In `render_review_comment`, inside the per-verdict loop, after `quote_note` is assigned:

```python
        if v["verdict"] == "flagged" and not v.get("mechanism_supported", True):
            quote_note += ", **claimed mechanism not found in transcript**"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/review.py tests/test_review.py
git commit -m "review: verify a claimed mechanism is actually stated in the transcript"
```

---

### Task 4: Prefer a mechanism when choosing the cell's statement

**Files:**
- Modify: `pipeline/propose.py` (`propose_stance_updates`)
- Test: `tests/test_propose.py`

**Interfaces:**
- Consumes: statements carrying `mechanism`.
- Produces: stance dicts carry `mechanism` when the chosen statement has the key.

This task carries most of the user-visible payoff: without it a confident-but-vague quote
keeps beating a specific one and the matrix stays vague even after the data improves.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_propose.py`:

```python
def test_stance_prefers_a_mechanism_over_higher_confidence():
    """A confident platitude must not outrank a concrete proposal."""
    evidence = {
        "id": "hit", "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": [
            {"candidate": "example-candidate-a", "topic": "zoning-reform",
             "stance": "supports", "summary": "Wants more housing.",
             "quote": "We need more housing.", "confidence": 0.95,
             "is_housing": True, "attribution_flag": False, "mechanism": None},
            {"candidate": "example-candidate-a", "topic": "zoning-reform",
             "stance": "supports", "summary": "Would end apartment bans.",
             "quote": "I will end apartment bans.", "confidence": 0.70,
             "is_housing": True, "attribution_flag": False,
             "mechanism": "End apartment bans"},
        ],
    }
    stances = propose.propose_stance_updates(evidence, today="2026-09-01")

    assert len(stances) == 1
    assert stances[0]["mechanism"] == "End apartment bans"
    assert stances[0]["citations"] == ["hit#1"], "cites the specific statement"


def test_confidence_still_decides_between_two_mechanisms():
    evidence = {
        "id": "hit", "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": [
            {"candidate": "example-candidate-a", "topic": "zoning-reform",
             "stance": "supports", "summary": "A.", "quote": "qa",
             "confidence": 0.60, "is_housing": True, "attribution_flag": False,
             "mechanism": "Upzone transit corridors"},
            {"candidate": "example-candidate-a", "topic": "zoning-reform",
             "stance": "supports", "summary": "B.", "quote": "qb",
             "confidence": 0.90, "is_housing": True, "attribution_flag": False,
             "mechanism": "End apartment bans"},
        ],
    }
    stances = propose.propose_stance_updates(evidence, today="2026-09-01")
    assert stances[0]["mechanism"] == "End apartment bans"


def test_a_statement_with_no_mechanism_key_produces_a_stance_without_one():
    """Pre-migration data must not gain a spurious null."""
    evidence = {
        "id": "hit", "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": [
            {"candidate": "example-candidate-a", "topic": "zoning-reform",
             "stance": "supports", "summary": "s", "quote": "q",
             "confidence": 0.9, "is_housing": True, "attribution_flag": False},
        ],
    }
    stances = propose.propose_stance_updates(evidence, today="2026-09-01")
    assert "mechanism" not in stances[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_propose.py -k mechanism -v`
Expected: FAIL — the first test picks the 0.95 vague statement and the stance has no
`mechanism` key.

- [ ] **Step 3: Implement**

Replace `propose_stance_updates` in `pipeline/propose.py` with:

```python
def _rank(stmt: dict) -> tuple[int, float]:
    """Prefer a statement that names a mechanism, then higher confidence.

    Without the first term a confident platitude ("we need more housing", 0.95)
    outranks a concrete proposal ("end apartment bans", 0.70) and the matrix cell
    stays vague even when specific evidence exists in the same source.
    """
    return (1 if stmt.get("mechanism") else 0, stmt.get("confidence", 0.0))


def propose_stance_updates(evidence: dict, *, today: str) -> list[dict]:
    """One stance per (candidate, topic), citing the most specific statement.

    "Most specific" is a mechanism first, confidence second — see ``_rank``.
    """
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    for i, stmt in enumerate(evidence["statements"]):
        key = (stmt["candidate"], stmt["topic"])
        if key not in best or _rank(stmt) > _rank(best[key][1]):
            best[key] = (i, stmt)

    stances = []
    for (candidate, topic), (idx, stmt) in best.items():
        stance = {
            "candidate": candidate,
            "topic": topic,
            "stance": stmt["stance"],
            "summary": stmt["summary"],
            "citations": [f"{evidence['id']}#{idx}"],
            "updated_date": today,
        }
        # Denormalized like `summary`, and only when the statement was assessed:
        # copying a key that isn't there would turn "not yet assessed" into "vague".
        if "mechanism" in stmt:
            stance["mechanism"] = stmt["mechanism"]
        schemas.validate(stance, "stance")
        stances.append(stance)
    return stances
```

Note `_rank` uses `stmt.get("mechanism")` truthiness, so `None`, `""` and a missing key all
rank equally low — the distinction between `null` and absent matters for rendering, not for
ranking.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass. Confirm `test_stance_proposals_pick_highest_confidence_per_candidate_topic`
still passes — its statements have no `mechanism`, so ranking falls through to confidence.

- [ ] **Step 5: Commit**

```bash
git add pipeline/propose.py tests/test_propose.py
git commit -m "propose: cite the most specific statement, not merely the most confident"
```

---

### Task 5: Migrate the already-committed statements

**Files:**
- Create: `scripts/backfill_mechanism.py`
- Test: `tests/test_backfill_mechanism.py`

**Interfaces:**
- Consumes: `pipeline.llm.OpenRouterLLM`, `pipeline.propose.write_stance`, `pipeline.config`.
- Produces: `assess_mechanism(quote, summary, *, llm, model) -> str | None` and
  `migrate(data_dir, *, llm, model) -> dict` returning `{"statements": int, "stances": int}`.

Deliberately a script, not a `pipeline` CLI subcommand: it runs once, and every subcommand
is something a future session may invoke by accident.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backfill_mechanism.py`:

```python
"""The one-time migration that adds `mechanism` to already-committed statements.

It must ONLY add a field. Re-extracting would re-fetch (some outlets intermittently
block CI IPs) and is non-deterministic — the same article has yielded 0, 2 and 3
statements — so it could silently drop human-approved statements or reorder them,
invalidating the citation indexes that point at them.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import backfill_mechanism as bm


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def complete_json(self, *, model, system, user):
        self.calls.append(user)
        return self.answer


def _evidence(tmp_path, statements):
    d = tmp_path / "media-hits" / "2026-08"
    d.mkdir(parents=True)
    path = d / "hit.json"
    path.write_text(json.dumps({
        "id": "hit", "url": "https://example.com/a", "outlet": "O",
        "media_type": "article", "title": "T", "published_date": "2026-08-02",
        "discovered_date": "2026-08-02", "transcript_ref": None,
        "statements": statements,
    }, indent=2))
    return path


STMT = {
    "candidate": "example-candidate-a", "topic": "zoning-reform",
    "stance": "supports", "summary": "Would end apartment bans.",
    "quote": "I will end apartment bans.", "locator": None,
    "confidence": 0.9, "is_housing": True, "attribution_flag": False,
}


def test_adds_mechanism_without_touching_anything_else(tmp_path):
    path = _evidence(tmp_path, [STMT])
    before = json.loads(path.read_text())

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    after = json.loads(path.read_text())
    assert after["statements"][0]["mechanism"] == "End apartment bans"
    for key in ("quote", "summary", "stance", "confidence", "attribution_flag"):
        assert after["statements"][0][key] == before["statements"][0][key]
    assert len(after["statements"]) == 1, "must not add or drop statements"


def test_records_null_when_no_mechanism_is_named(tmp_path):
    path = _evidence(tmp_path, [dict(STMT, quote="Chicago needs more housing.")])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    assert json.loads(path.read_text())["statements"][0]["mechanism"] is None


def test_statement_order_is_preserved(tmp_path):
    """Citations pin an index; reordering would silently repoint every one."""
    a = dict(STMT, quote="first quote")
    b = dict(STMT, quote="second quote", topic="adus")
    path = _evidence(tmp_path, [a, b])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": None}), model="m")

    after = json.loads(path.read_text())["statements"]
    assert [s["quote"] for s in after] == ["first quote", "second quote"]


def test_output_is_schema_valid(tmp_path):
    from pipeline import schemas
    path = _evidence(tmp_path, [STMT])

    bm.migrate(tmp_path, llm=FakeLLM({"mechanism": "End apartment bans"}), model="m")

    schemas.validate(json.loads(path.read_text()), "evidence")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backfill_mechanism.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts'`.

- [ ] **Step 3: Write the script**

Create `scripts/__init__.py` (empty, so the tests can import it).

Create `scripts/backfill_mechanism.py`:

```python
"""One-time migration: add `mechanism` to already-committed statements.

Reads committed evidence, asks the model what mechanism each quote names, and
writes the field. It ONLY adds a field — no re-fetch, no re-extraction, no
statement added, removed or reordered. Reordering would repoint every citation,
which pins a statement index.

Usage:
    set -a && . ./.env && set +a
    .venv/bin/python -m scripts.backfill_mechanism data
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pipeline import config, schemas

SYSTEM = (
    "You are told a quote from a candidate and the summary drawn from it. "
    "Identify the concrete policy mechanism the QUOTE names: a program, a rule "
    "change, a funding source, a quantity, or a deadline. Respond as JSON: "
    '{"mechanism": "<short phrase>"} or {"mechanism": null}. '
    "Use null when the quote expresses only a goal, a value, or a direction "
    "without saying how — 'housing should be affordable', 'build more housing', "
    "'ensure residents aren't displaced' are all null. Directional phrasing "
    "counts ONLY if the quote names what would change: 'streamline permitting' "
    "is null, 'cut permit review to 30 days' is a mechanism. When in doubt use "
    "null; never invent specificity the quote does not contain."
)


def assess_mechanism(quote: str, summary: str, *, llm, model: str) -> str | None:
    answer = llm.complete_json(
        model=model, system=SYSTEM,
        user=f"Summary: {summary}\nQuote: {quote}",
    )
    mechanism = answer.get("mechanism")
    return mechanism if isinstance(mechanism, str) and mechanism.strip() else None


def migrate(data_dir, *, llm, model: str) -> dict:
    data_dir = Path(data_dir)
    n_statements = 0

    for path in sorted((data_dir / "media-hits").rglob("*.json")):
        evidence = json.loads(path.read_text())
        changed = False
        for stmt in evidence["statements"]:          # order preserved in place
            if "mechanism" in stmt:
                continue
            stmt["mechanism"] = assess_mechanism(
                stmt["quote"], stmt["summary"], llm=llm, model=model)
            n_statements += 1
            changed = True
        if changed:
            schemas.validate(evidence, "evidence")
            path.write_text(json.dumps(evidence, indent=2) + "\n")

    n_stances = _refresh_stances(data_dir)
    return {"statements": n_statements, "stances": n_stances}


def _refresh_stances(data_dir: Path) -> int:
    """Copy each cell's cited statement's mechanism onto the cell."""
    by_id = {}
    for path in sorted((data_dir / "media-hits").rglob("*.json")):
        ev = json.loads(path.read_text())
        by_id[ev["id"]] = ev

    n = 0
    stance_dir = data_dir / "stances"
    for path in sorted(stance_dir.rglob("*.json")) if stance_dir.exists() else []:
        stance = json.loads(path.read_text())
        # The last citation is the most recently proposed, so it is the one whose
        # statement produced this cell's current stance and summary.
        ev_id, _, idx = stance["citations"][-1].rpartition("#")
        ev = by_id.get(ev_id)
        if not ev or not idx.isdigit() or int(idx) >= len(ev["statements"]):
            continue
        stmt = ev["statements"][int(idx)]
        if "mechanism" not in stmt or stance.get("mechanism") == stmt["mechanism"]:
            continue
        stance["mechanism"] = stmt["mechanism"]
        schemas.validate(stance, "stance")
        path.write_text(json.dumps(stance, indent=2) + "\n")
        n += 1
    return n


def main(argv: list[str]) -> int:
    from pipeline.llm import OpenRouterLLM

    data_dir = Path(argv[1] if len(argv) > 1 else "data")
    cfg = config.load_config(data_dir)
    counts = migrate(data_dir, llm=OpenRouterLLM(),
                     model=cfg["models"]["extractor"])
    print(f"statements={counts['statements']} stances={counts['stances']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

Note `_refresh_stances` writes the file directly rather than calling
`propose.write_stance`, because `write_stance` would union citations and rewrite
`updated_date`; this migration must change exactly one field.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Run the migration for real and review the diff**

```bash
set -a && . ./.env && set +a
.venv/bin/python -m scripts.backfill_mechanism data
git diff --stat
git diff data/stances/brandon-johnson/
```

Expected: Johnson's cells gain real mechanism strings ("Freeze transfers of CHA land to
non-housing uses"); Giannoulias, Brewer and Holberg's gain `null`. **Read the diff before
committing** — if a Johnson cell comes back `null`, the migration prompt is too strict and
should be fixed before this lands. Costs a few cents for ~50 statements.

- [ ] **Step 6: Commit**

```bash
git add scripts/ tests/test_backfill_mechanism.py data/
git commit -m "migrate: assess a mechanism for every committed statement"
```

---

### Task 6: Show the difference on the site

**Files:**
- Modify: `site/src/pages/index.astro` (matrix cell + legend)
- Modify: `site/src/pages/candidates/[slug].astro` (per-topic display)
- Modify: `site/src/pages/methodology.astro` (state the bar)
- Test: `site/src/lib/data.test.js`

**Interfaces:**
- Consumes: `stance.mechanism` from Task 4/5 via the existing `buildMatrix()` and
  `buildCandidateProfile()` return shapes — `cell.stance.mechanism` and
  `position.stance.mechanism`. No change to `data.js` is required; the field rides along on
  the stance object already returned.

- [ ] **Step 1: Write the failing test**

Append to `site/src/lib/data.test.js`, matching that file's existing `node:test` style:

```javascript
test("matrix cells expose the mechanism so vague positions can be marked", () => {
  const { rows } = buildMatrix();
  const cells = rows.flatMap((r) => r.cells).filter((c) => c.stance);
  // A cell that has been assessed carries the key; null means "no specifics".
  const assessed = cells.filter((c) => "mechanism" in c.stance);
  for (const c of assessed) {
    const m = c.stance.mechanism;
    assert.ok(m === null || typeof m === "string",
      "mechanism must be a string or null, never undefined-as-value");
  }
});
```

- [ ] **Step 2: Run test to verify it passes or fails meaningfully**

Run: `cd site && node --test`
Expected: passes once Task 5's data has landed (it asserts a property of real data). If it
fails, `data.js` is stripping unknown stance keys — fix that before continuing.

- [ ] **Step 3: Render the distinction**

In `site/src/pages/index.astro`, where a cell's stance is rendered, add a marker when the
cell was assessed and has no mechanism:

```astro
{cell.stance && "mechanism" in cell.stance && cell.stance.mechanism === null && (
  <span class="no-mechanism" title="No specific mechanism stated">&#9679;</span>
)}
```

with CSS that mutes the cell, and a legend line near the existing key:

```html
<p class="legend"><span class="no-mechanism">&#9679;</span> Supports the goal but named no specific mechanism.</p>
```

In `site/src/pages/candidates/[slug].astro`, under each position:

```astro
{"mechanism" in position.stance && (
  position.stance.mechanism
    ? <p class="mechanism"><strong>How:</strong> {position.stance.mechanism}</p>
    : <p class="mechanism none">No specific mechanism stated.</p>
)}
```

In `site/src/pages/methodology.astro`, add a section stating the bar verbatim from the spec:
a mechanism is a program, rule change, funding source, quantity, or deadline, supported by
the quote; vague positions are shown and marked rather than hidden, because a blank cell
cannot distinguish "no coverage found" from "commits to nothing"; and where that looks
unfair to a candidate the pipeline has covered little of, the remedy is more coverage, not
a softer bar.

- [ ] **Step 4: Verify the build and the links**

Run: `cd site && npm run build && node --test`
Expected: build succeeds, tests pass. Check the built matrix shows Johnson's column
unmarked and Giannoulias's marked.

- [ ] **Step 5: Commit**

```bash
git add site/
git commit -m "site: mark positions that name no policy mechanism"
```

---

### Task 7: Propagate the bar to the nine open backfill issues

**Files:**
- Modify: `CLAUDE.md` (Data model + Common changes sections)
- No code. Uses `gh issue comment`.

**Interfaces:**
- Consumes: the shipped behaviour from Tasks 1–6.
- Produces: nothing code-facing. This exists so the nine un-run backfills apply the new
  standard instead of seeding more platitudes.

Without this the feature is inert for the work that is actually queued: #51 and #53–#60
have not run yet, and their issue text still describes the old bar.

- [ ] **Step 1: Document the field in CLAUDE.md**

In the **Data model** section, after the `Stance enum` line, add:

```markdown
- **Mechanism** (optional `mechanism` on a statement and on a stance) — the concrete
  instrument the candidate named: a program, rule change, funding source, quantity, or
  deadline, supported by the quote. `null` means assessed and none offered; an absent key
  means not yet assessed, which is deliberately different. "Supports affordable housing" is
  not a position — every candidate says it — so the matrix marks a cell whose cited
  statement named no mechanism, and `propose_stance_updates` cites the most *specific*
  statement rather than merely the most confident. Borderline cases resolve to `null`:
  inventing specificity is the failure that matters. The reviewer verifies a claimed
  mechanism is actually in the transcript, which is why the field captures the mechanism
  rather than grading specificity — a claim about the transcript is checkable, a subjective
  grade is not.
```

In **Common changes**, under the local-backfill how-to, add:

```markdown
  **Check the mechanisms before opening the PR.** `grep -o '"mechanism": [^,]*'` over the
  new evidence files. A candidate whose rows all come back `null` has genuinely said
  nothing specific — that is a finding, not a bug, and the row should still ship. But if a
  row you know contains a concrete proposal comes back `null`, re-read the quote: the
  extractor may have picked a vaguer sentence than the one that mattered.
```

- [ ] **Step 2: Commit the doc change**

```bash
git add CLAUDE.md
git commit -m "docs: record the mechanism bar for backfills"
```

- [ ] **Step 3: Comment on each open backfill issue**

Run this once, after Tasks 1–6 are merged to `main`:

```bash
for n in 51 53 54 55 56 57 58 59 60; do
  gh issue comment "$n" --body 'Standard raised before this issue runs (spec #74, plan in `docs/superpowers/plans/2026-09-01-mechanism-specificity.md`).

Statements now carry a `mechanism`: the concrete instrument the candidate named — a program, rule change, funding source, quantity, or deadline — supported by the quote, or `null` if they named none. `null` is expected and correct for a candidate who has only voiced support; those rows still ship, and the matrix marks them. **Do not stretch a quote to fill the field** — the reviewer verifies a claimed mechanism is actually in the transcript, and an invented one is flagged.

Two consequences for how this issue should be worked:

1. **When sourcing rows, prefer material where the candidate says how.** Platform pages, policy interviews and candidate forums carry mechanisms; launch-day press mostly does not. A candidate seeded only from announcement coverage will correctly show as having no specifics, which is a thin result rather than a wrong one.
2. **Before opening the PR, `grep -o '"'"'"mechanism": [^,]*'"'"' data/media-hits/*/*.json`.** All-`null` for a candidate with a real published platform means the wrong sources were picked, not that the candidate is vague.

The `record` requirement for officeholders is unchanged.'
done
```

- [ ] **Step 4: Verify**

Run: `gh issue view 51 --json comments --jq '.comments[-1].body' | head -5`
Expected: the comment above.

---

## Verification

- Offline: `.venv/bin/pytest` (164 passing before this plan; expect ~178 after) and
  `cd site && node --test`.
- Live, after Task 5: the migration diff shows Johnson specific and Giannoulias `null`.
- End-to-end, after Task 6: `cd site && npm run build`, then confirm the matrix visibly
  distinguishes the two columns.
- The real acceptance test is editorial, not mechanical: open the built matrix and check
  that "who has an actual plan" is answerable at a glance. If it is not, the rendering in
  Task 6 is too subtle.
