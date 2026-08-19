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
