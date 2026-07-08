// Smoke tests for the build-time data layer, run with `node --test`.
// They exercise the real data/ tree so a bad merge that breaks the site build
// is caught here too.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  loadCandidates,
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
  const slug = loadCandidates()[0].slug;
  const profile = buildCandidateProfile(slug);
  assert.equal(profile.candidate.slug, slug);
  assert.equal(buildCandidateProfile("nobody-here"), null);
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
