// Build-time data layer: reads the repo's data/ tree and shapes it for the site.
// No network, no DB — just JSON files on disk, read once at build.
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// site/src/lib -> repo root is three up, then data/
export const DATA_DIR =
  process.env.DATA_DIR || join(HERE, "..", "..", "..", "data");

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function walkJson(dir) {
  const out = [];
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkJson(p));
    else if (entry.name.endsWith(".json")) out.push(p);
  }
  return out;
}

export function loadCandidates() {
  return readJson(join(DATA_DIR, "registry", "candidates.json")).candidates;
}

export function loadTopics() {
  const topics = readJson(join(DATA_DIR, "registry", "topics.json")).topics;
  return [...topics].sort((a, b) => (a.order ?? 999) - (b.order ?? 999));
}

export function loadEvidence() {
  return walkJson(join(DATA_DIR, "media-hits")).map(readJson);
}

export function loadStances() {
  return walkJson(join(DATA_DIR, "stances")).map(readJson);
}

export function evidenceIndex(evidence = loadEvidence()) {
  const idx = {};
  for (const e of evidence) idx[e.id] = e;
  return idx;
}

// "<evidence-id>#<index>" -> { statement, evidence } (or null if dangling)
export function resolveCitation(citation, index) {
  const hash = citation.lastIndexOf("#");
  if (hash < 0) return null;
  const id = citation.slice(0, hash);
  const i = Number(citation.slice(hash + 1));
  const evidence = index[id];
  if (!evidence || !Array.isArray(evidence.statements)) return null;
  const statement = evidence.statements[i];
  if (!statement) return null;
  return { statement, evidence };
}

// The homepage matrix: topics (rows) x candidates (cols), each cell a stance.
export function buildMatrix() {
  const candidates = loadCandidates();
  const topics = loadTopics();
  const stances = loadStances();
  const idx = evidenceIndex();

  const byKey = {};
  for (const s of stances) byKey[`${s.candidate}::${s.topic}`] = s;

  const rows = topics.map((topic) => ({
    topic,
    cells: candidates.map((candidate) => {
      const stance = byKey[`${candidate.slug}::${topic.slug}`] || null;
      const sources = stance
        ? stance.citations
            .map((c) => resolveCitation(c, idx))
            .filter(Boolean)
        : [];
      return { candidate, stance, sources };
    }),
  }));

  return { candidates, topics, rows };
}

// Candidate profile: their stances grouped by topic + a chronological timeline.
export function buildCandidateProfile(slug) {
  const candidate = loadCandidates().find((c) => c.slug === slug);
  if (!candidate) return null;
  const topics = loadTopics();
  const stances = loadStances().filter((s) => s.candidate === slug);
  const idx = evidenceIndex();

  const byTopic = {};
  for (const s of stances) byTopic[s.topic] = s;

  const positions = topics
    .map((topic) => {
      const stance = byTopic[topic.slug];
      if (!stance) return null;
      const sources = stance.citations
        .map((c) => resolveCitation(c, idx))
        .filter(Boolean);
      return { topic, stance, sources };
    })
    .filter(Boolean);

  const timeline = loadEvidence()
    .filter((e) => e.statements.some((st) => st.candidate === slug))
    .sort((a, b) => b.published_date.localeCompare(a.published_date));

  return { candidate, positions, timeline };
}

// Reverse-chronological feed of media hits.
export function buildFeed() {
  return loadEvidence().sort((a, b) =>
    b.published_date.localeCompare(a.published_date),
  );
}

export const STANCE_META = {
  supports: { label: "Supports", tone: "support" },
  "supports-with-conditions": { label: "Supports w/ conditions", tone: "conditional" },
  mixed: { label: "Mixed / unclear", tone: "mixed" },
  opposes: { label: "Opposes", tone: "oppose" },
  "no-position": { label: "No stated position", tone: "none" },
};
