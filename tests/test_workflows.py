"""Contract tests for `.github/workflows/backfill.yml` (#63).

The backfill workflow is how a backfill runs with credentials in CI: the CLI needs
OPENROUTER_API_KEY (and GROQ_API_KEY for audio rows), a cloud Claude session has
no secrets store, and this repo is public so keys can never live in it. (Since
2026-09-01 it is no longer the only path — the same CLI also runs locally against
a gitignored .env, which is required for outlets that block datacenter IPs. That
path bypasses this workflow entirely, so these tests still pin the CI one.) That
makes the workflow's CLI invocation and its slug-planning heredoc load-bearing — the previous version's only two recorded runs both failed and it
was deleted, so pin its contract here instead of rediscovering it live.

These are offline checks: YAML shape, flags that must exist in the `backfill`
subparser, and the plan job's heredoc executed against the real phase files.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pipeline.__main__ import build_parser, cmd_backfill

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "backfill.yml"
PHASE1 = REPO / "data" / "backfill" / "phase1.json"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _step(job: dict, *, step_id: str | None = None, contains: str | None = None) -> dict:
    for step in job["steps"]:
        if step_id and step.get("id") == step_id:
            return step
        if contains and contains in (step.get("run") or ""):
            return step
    raise AssertionError(f"no step matching id={step_id!r} contains={contains!r}")


def _heredoc(run: str) -> str:
    m = re.search(r"<<'PY'[^\n]*\n(.*?)\n\s*PY\b", run, re.DOTALL)
    assert m, "expected a fixed `python - <<'PY'` heredoc (untrusted input never in shell)"
    return m.group(1)


def _outputs(stdout: str) -> dict:
    """Parse the `key=value` lines the heredoc appends to $GITHUB_OUTPUT."""
    return dict(line.partition("=")[::2] for line in stdout.strip().splitlines())


def _run_heredoc(src: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-"], input=src, text=True, capture_output=True,
        cwd=REPO, env={**os.environ, **env}, check=False,
    )


def test_workflow_exists_and_parses(wf):
    assert wf["name"]
    # `on:` is parsed by PyYAML 1.1 rules as the boolean True.
    triggers = wf.get("on") or wf[True]
    assert "workflow_dispatch" in triggers, "must stay hand-run; no schedule"
    assert "schedule" not in triggers


def test_dispatch_inputs(wf):
    inputs = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    # The old version hardcoded phase1.json in two places, so it could not serve
    # ten per-candidate issues (#50).
    assert inputs["phase_file"]["default"] == "data/backfill/phase1.json"
    assert "slugs" in inputs


def test_backfill_cli_invocation_matches_the_subparser(wf):
    """Every flag the workflow passes must exist, or the run dies after paying for LLM calls."""
    step = _step(wf["jobs"]["backfill"], contains="python -m pipeline backfill")
    cmd = step["run"].replace("\\\n", " ")
    line = next(ln for ln in cmd.splitlines() if "python -m pipeline backfill" in ln)
    argv = shlex.split(line.replace('"$PHASE_FILE"', str(PHASE1)).replace('"$SLUG"', "brandon-johnson"))
    assert argv[:3] == ["python", "-m", "pipeline"]

    args = build_parser().parse_args(argv[3:])
    assert args.func is cmd_backfill
    assert args.input == str(PHASE1)
    assert args.only == "brandon-johnson"
    assert args.skip_ledger is True, "matrix jobs must not race on data/ledger.json"


def test_only_flag_matches_the_key_used_in_the_phase_files():
    for phase in sorted((REPO / "data" / "backfill").glob("*.json")):
        raw = json.loads(phase.read_text())
        rows = raw["rows"] if isinstance(raw, dict) else raw
        assert rows, f"{phase.name} has no rows"
        for row in rows:
            assert row["candidate_slug"], f"{phase.name}: row missing candidate_slug"


def test_plan_heredoc_emits_slugs_for_the_real_phase_file(wf):
    src = _heredoc(_step(wf["jobs"]["plan"], step_id="slugs")["run"])
    proc = _run_heredoc(src, PHASE_FILE=str(PHASE1.relative_to(REPO)), ONLY_SLUGS="")
    assert proc.returncode == 0, proc.stderr

    out = _outputs(proc.stdout)
    expected = sorted({r["candidate_slug"] for r in json.loads(PHASE1.read_text())["rows"]})
    assert json.loads(out["slugs"]) == expected
    # The matrix job reuses the validated path rather than re-reading the raw input.
    assert out["phase_file"] == str(PHASE1.relative_to(REPO))


def test_plan_heredoc_applies_the_slugs_filter(wf):
    src = _heredoc(_step(wf["jobs"]["plan"], step_id="slugs")["run"])
    proc = _run_heredoc(
        src, PHASE_FILE=str(PHASE1.relative_to(REPO)), ONLY_SLUGS="mike-quigley, joe-holberg",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(_outputs(proc.stdout)["slugs"]) == ["joe-holberg", "mike-quigley"]


def test_plan_heredoc_fails_loudly_on_an_unknown_slug(wf):
    """An empty matrix makes `fromJSON` produce zero jobs and the run looks green."""
    src = _heredoc(_step(wf["jobs"]["plan"], step_id="slugs")["run"])
    proc = _run_heredoc(src, PHASE_FILE=str(PHASE1.relative_to(REPO)), ONLY_SLUGS="not-a-candidate")
    assert proc.returncode != 0
    assert "not-a-candidate" in (proc.stderr + proc.stdout)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "data/registry/config.json"])
def test_plan_heredoc_refuses_a_phase_file_outside_the_backfill_dir(wf, bad):
    """`phase_file` is dispatch input; it must not become an arbitrary read."""
    src = _heredoc(_step(wf["jobs"]["plan"], step_id="slugs")["run"])
    proc = _run_heredoc(src, PHASE_FILE=bad, ONLY_SLUGS="")
    assert proc.returncode != 0, f"{bad} was accepted"


def test_dispatch_input_never_reaches_the_shell(wf):
    """Same discipline as intake.yml: env var in, fixed heredoc parses it."""
    for job in wf["jobs"].values():
        for step in job["steps"]:
            run = step.get("run") or ""
            assert "${{ github.event.inputs" not in run
            assert "${{ inputs." not in run


def test_one_candidate_at_a_time(wf):
    # Credit-limited, and the agreed discipline is one candidate merged before
    # the next starts.
    assert wf["jobs"]["backfill"]["strategy"]["max-parallel"] == 1
    assert wf["jobs"]["backfill"]["strategy"]["fail-fast"] is False


def test_no_ledger_seeding_job(wf):
    """Marking every phase URL seen burns URLs whose PR never merged (#42, #61)."""
    assert "seed-ledger" not in wf["jobs"]
    assert "mark_all" not in WORKFLOW.read_text()


def test_backfill_step_has_both_keys(wf):
    """Podcast enclosures are the richest source for this track, and need Whisper."""
    step = _step(wf["jobs"]["backfill"], contains="python -m pipeline backfill")
    assert "OPENROUTER_API_KEY" in step["env"]
    assert "GROQ_API_KEY" in step["env"]
    install = _step(wf["jobs"]["backfill"], contains="pip install")
    assert "ffmpeg" in install["run"], "audio rows downsample before upload"
    assert '".[live]"' in install["run"], "yt-dlp is needed for audio rows"


def test_pr_is_opened_with_the_pat_and_a_per_run_branch(wf):
    pr = next(s for s in wf["jobs"]["backfill"]["steps"]
              if "create-pull-request" in (s.get("uses") or ""))
    # GITHUB_TOKEN-created PRs do not fire review.yml.
    assert "PIPELINE_PAT" in pr["with"]["token"]
    assert "pipeline" in pr["with"]["labels"]
    assert pr["with"]["add-paths"] == "data", "globs fail the git add when a subdir is empty"
    branch = pr["with"]["branch"]
    assert branch.startswith("backfill/")
    # A fixed branch + an unmerged PR = create-pull-request force-rebuilds and
    # destroys the previous run's files (the #62 clobber).
    assert "github.run_number" in branch


def _backfill_step_id(wf: dict) -> str:
    step = _step(wf["jobs"]["backfill"], contains="python -m pipeline backfill")
    assert step.get("id"), "the backfill step needs an id so later steps can read its outcome"
    return step["id"]


def test_a_failed_row_does_not_discard_the_rows_that_succeeded(wf):
    """`cmd_backfill` exits 1 if *any* row errored, so a bare `if: success()` on
    the PR step throws away every file the other rows already paid for. Phase
    files for the per-candidate track (#50) are multi-URL, and #41 (Block Club /
    Reader 429s) plus #32 (the YouTube bot-gate) make one dead row the expected
    case. `--skip-ledger` means nothing was marked, so a re-run re-pays for the
    rows that already worked.
    """
    step_id = _backfill_step_id(wf)
    step = _step(wf["jobs"]["backfill"], step_id=step_id)
    assert step.get("continue-on-error") is True

    pr = next(s for s in wf["jobs"]["backfill"]["steps"]
              if "create-pull-request" in (s.get("uses") or ""))
    # Ungated on purpose: create-pull-request no-ops on a zero diff, so a run
    # where *every* row failed still opens nothing.
    assert "if" not in pr, "the PR step must not be gated on the backfill step succeeding"


def test_a_failed_row_still_fails_the_job(wf):
    """Keeping the partial work must not make a broken row look green (#42's
    lesson one layer down: a green run is not evidence the work was kept)."""
    step_id = _backfill_step_id(wf)
    steps = wf["jobs"]["backfill"]["steps"]
    gate = next(
        (s for s in steps if f"steps.{step_id}.outcome" in (s.get("if") or "")),
        None,
    )
    assert gate is not None, "no step re-raises the backfill failure"
    assert "failure" in gate["if"]
    assert "exit 1" in (gate.get("run") or "")

    pr_i = next(i for i, s in enumerate(steps) if "create-pull-request" in (s.get("uses") or ""))
    assert steps.index(gate) > pr_i, "fail the job only after the partial work is captured"


REVIEW_WORKFLOW = REPO / ".github" / "workflows" / "review.yml"


def test_review_fires_when_the_pipeline_label_is_added():
    """`review.yml` must trigger on `labeled`, not just `opened`/`synchronize`.

    The verify job is gated on the PR carrying the `pipeline` label, but the
    `opened` event payload is built before a label applied at creation time lands
    — so `gh pr create --label pipeline` opens a PR whose `opened` run sees an
    empty label list and skips. Observed live on PR #96 (#57): tests ran, the
    reviewer silently did nothing, and the PR sat with no verdict label at all.
    That is the same silent-failure shape as a green cron that published nothing:
    the workflow is "successful" precisely because it did no work.

    `labeled` is also the *safer* trigger of the two, not a loosening — applying a
    label requires write access on the repo, while opening a PR does not.
    """
    wf = yaml.safe_load(REVIEW_WORKFLOW.read_text())
    on = wf.get("on") or wf.get(True)          # YAML 1.1 parses bare `on:` as True
    types = on["pull_request"]["types"]
    assert "labeled" in types, (
        "a PR opened with --label pipeline would never be reviewed; "
        f"types are {types}"
    )
    assert "synchronize" in types              # still re-review on every push
