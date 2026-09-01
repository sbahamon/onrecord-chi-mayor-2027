// Smoke tests for the build-time data layer, run with `node --test`.
// They exercise the real data/ tree so a bad merge that breaks the site build
// is caught here too.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  loadCandidates,
  OUTCOME_META,
  loadTrackedCandidates,
  loadDroppedCandidates,
  loadTopics,
  buildMatrix,
  buildCandidateProfile,
  buildFeed,
  resolveCitation,
  evidenceIndex,
} from "./data.js";

test("candidates and topics load", () => {
  assert.ok(loadCandidates().length >= 1);
  assert.ok(loadTopics().length >= 1);
});

test("topics come back ordered", () => {
  const orders = loadTopics().map((t) => t.order ?? 999);
  const sorted = [...orders].sort((a, b) => a - b);
  assert.deepEqual(orders, sorted);
});

test("matrix has one row per topic and one cell per candidate", () => {
  const { rows, candidates, topics } = buildMatrix();
  assert.equal(rows.length, topics.length);
  for (const row of rows) assert.equal(row.cells.length, candidates.length);
});

test("every matrix cell with a stance resolves at least one source", () => {
  const { rows } = buildMatrix();
  for (const row of rows) {
    for (const cell of row.cells) {
      if (cell.stance) {
        assert.ok(
          cell.sources.length >= 1,
          `dead citation in ${cell.candidate.slug}/${row.topic.slug}`,
        );
      }
    }
  }
});

test("candidate profile builds and unknown slug returns null", () => {
  const slug = loadTrackedCandidates()[0].slug;
  const profile = buildCandidateProfile(slug);
  assert.equal(profile.candidate.slug, slug);
  assert.equal(buildCandidateProfile("nobody-here"), null);
});

test("dropped candidates are excluded from the matrix but listed separately", () => {
  const tracked = loadTrackedCandidates();
  const dropped = loadDroppedCandidates();
  // tracked + dropped partition the full roster
  assert.equal(tracked.length + dropped.length, loadCandidates().length);
  // a dropped candidate has a reason and is not a matrix column
  for (const c of dropped) {
    assert.equal(c.tracked, false);
    assert.ok(typeof c.drop_reason === "string" && c.drop_reason.length > 0);
  }
  const matrixSlugs = buildMatrix().candidates.map((c) => c.slug);
  for (const c of dropped) assert.ok(!matrixSlugs.includes(c.slug));
  // a dropped candidate builds no profile page
  for (const c of dropped) assert.equal(buildCandidateProfile(c.slug), null);
});

test("feed is sorted newest first", () => {
  const feed = buildFeed();
  for (let i = 1; i < feed.length; i++) {
    assert.ok(feed[i - 1].published_date >= feed[i].published_date);
  }
});

test("resolveCitation returns null for a dangling citation", () => {
  assert.equal(resolveCitation("nope#0", evidenceIndex()), null);
});

test("every record outcome in the schema has a site label", () => {
  // The stance schema's outcome enum and the site's label map must not drift:
  // an unmapped outcome would silently render as a bare slug on a public page.
  const schema = JSON.parse(
    readFileSync(new URL("../../../schemas/stance.schema.json", import.meta.url)),
  );
  const outcomes = schema.properties.record.items.properties.outcome.enum;
  for (const o of outcomes) {
    assert.ok(OUTCOME_META[o], `no OUTCOME_META entry for outcome "${o}"`);
    assert.ok(OUTCOME_META[o].label && OUTCOME_META[o].tone);
  }
  assert.deepEqual(Object.keys(OUTCOME_META).sort(), [...outcomes].sort());
});

test("a defeat is renderable, not hidden", () => {
  // Guards the point of the field: failures are part of the record.
  assert.equal(OUTCOME_META.failed.label, "Failed");
});

// --- mechanism: policy specificity ------------------------------------------
// "Supports affordable housing" is not a position. A cell whose cited statement
// named no mechanism must be distinguishable from one that named a real one, and
// from one not yet assessed — three states, not two.

test("matrix cells carry the mechanism, and it is a string or null when assessed", () => {
  const cells = buildMatrix()
    .rows.flatMap((r) => r.cells)
    .filter((c) => c.stance && "mechanism" in c.stance);

  assert.ok(cells.length > 0, "migration has run, so cells should be assessed");
  for (const c of cells) {
    const m = c.stance.mechanism;
    assert.ok(
      m === null || (typeof m === "string" && m.length > 0),
      `mechanism must be a non-empty string or null, got ${JSON.stringify(m)}`
    );
  }
});

test("the matrix distinguishes specific positions from vague ones", () => {
  const cells = buildMatrix()
    .rows.flatMap((r) => r.cells)
    .filter((c) => c.stance && "mechanism" in c.stance);
  const specific = cells.filter((c) => c.stance.mechanism !== null);
  const vague = cells.filter((c) => c.stance.mechanism === null);

  // Both groups must be non-empty or the rendering has nothing to distinguish —
  // and a bug that collapsed every cell into one bucket would pass a weaker test.
  assert.ok(specific.length > 0, "expected some cells to name a mechanism");
  assert.ok(vague.length > 0, "expected some cells to name none");
});

test("candidate profiles expose the mechanism for each position", () => {
  const profile = buildCandidateProfile("brandon-johnson");
  const assessed = profile.positions.filter((p) => "mechanism" in p.stance);

  assert.ok(assessed.length > 0);
  // The incumbent has a published platform: every one of his cells is specific.
  // If this ever goes null the migration prompt has regressed, not the data.
  for (const p of assessed) {
    assert.equal(typeof p.stance.mechanism, "string", `${p.topic.slug} lost its mechanism`);
  }
});
